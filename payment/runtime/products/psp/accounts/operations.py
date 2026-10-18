"""Procurement, foreign exchange, evidence and access operations."""

from copy import deepcopy

OPERATIONS = {}


def operation(name, description, properties, required=None, read_only=False):

    def register(fn):
        OPERATIONS[name] = {
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties) if required is None else required,
                "additionalProperties": False,
            },
            "read_only": read_only,
            "function": fn,
        }
        return fn

    return register


S = {"type": "string"}
I = {"type": "integer", "minimum": 0}
A = {"type": "array", "items": S}
B = {"type": "boolean"}


def debit(ctx, row, amount, kind):
    return ctx.ledger(
        row["account_id"],
        -amount,
        row.get("source_currency", row.get("currency", "USD")),
        kind,
        row["id"],
    )


def history(ctx, kind, row, **fields):
    return ctx.create(
        "xp_b_history",
        customer_id=row["customer_id"],
        kind=kind,
        subject_id=row["id"],
        minute=ctx.minute,
        **fields,
    )


def require_document(ctx, target_id, purpose, predicate):
    submissions = ctx.uploaded(target_id, purpose)
    ctx.require(bool(submissions), "DOCUMENT_SUBMISSION_REQUIRED")
    ctx.require(
        any(
            (
                isinstance(x.get("content"), dict)
                and x["content"].get("document_type") == purpose
                and predicate(x["content"])
                for x in submissions
            )
        ),
        "DOCUMENT_CONTENT_MISMATCH",
    )


@operation(
    "settle_export_proceeds",
    "Withdraw an unsettled export lot. lot_id and destination_id identify published records. product is ordinary or declaration. The full lot is converted using its integer rate numerator/denominator; fee_minor is the published product fee, debited in source currency. Ordinary withdrawal never creates declaration rights.",
    {"lot_id": S, "destination_id": S, "product": S},
)
def settle_export(ctx, lot_id, destination_id, product):
    r = ctx.record("xp_b_export_lots", lot_id)
    d = ctx.record("xp_b_destinations", destination_id)
    ctx.require(r["status"] == "available", "LOT_NOT_AVAILABLE")
    ctx.require(product in ("ordinary", "declaration"), "PRODUCT")
    if product == "declaration":
        ctx.require(r.get("declaration_eligible", True), "LOT_NOT_REPORTABLE")
        ctx.require(
            d["owner"] == r["exporter"] and d["kind"] == "corporate", "SAME_ENTITY_REQUIRED"
        )
    fee = r["fees_minor"][product]
    debit(ctx, r, r["amount_minor"] + fee, "export_settlement")
    proceeds = r["amount_minor"] * r["rate_num"] // r["rate_den"]
    ctx.update(
        "xp_b_export_lots",
        lot_id,
        status="settled",
        product=product,
        destination_id=destination_id,
        proceeds_minor=proceeds,
        fee_minor=fee,
    )
    ctx.update(
        "xp_b_destinations", destination_id, received_minor=d.get("received_minor", 0) + proceeds
    )
    history(
        ctx,
        "export_settled",
        r,
        product=product,
        destination_id=destination_id,
        amount_minor=r["amount_minor"],
        fee_minor=fee,
    )
    if product == "declaration":
        ctx.schedule("b_declaration", ctx.minute + r["declaration_delay"], lot_id)
    return {
        "lot_id": lot_id,
        "proceeds_minor": proceeds,
        "declaration_status": "pending" if product == "declaration" else "ineligible",
    }


@operation(
    "deliver_declaration_record",
    "Deliver an actually issued export declaration record for the named lot. A settled ordinary or historical withdrawal cannot be converted into a declaration.",
    {"lot_id": S},
)
def deliver_declaration(ctx, lot_id):
    r = ctx.record("xp_b_export_lots", lot_id)
    ctx.require(r.get("declaration_status") == "issued", "DECLARATION_NOT_ISSUED")
    ctx.update("xp_b_export_lots", lot_id, declaration_delivered=True)
    return {"lot_id": lot_id, "declaration_id": r["declaration_id"], "delivered": True}


@operation(
    "void_fee_invoice",
    "Void an issued synthetic service-fee invoice before replacement; a delivered invoice incurs its published void fee_minor. Fee occupancy is released only by this operation.",
    {"invoice_id": S},
)
def void_invoice(ctx, invoice_id):
    r = ctx.record("xp_b_invoices", invoice_id)
    ctx.require(r["status"] in ("issued", "delivered"), "INVOICE_NOT_ACTIVE")
    if r["status"] == "delivered" and r.get("void_fee_minor", 0):
        debit(ctx, r, r["void_fee_minor"], "invoice_void_fee")
    ctx.update("xp_b_invoices", invoice_id, status="void")
    for f in ctx.records("xp_b_fees"):
        if f.get("invoice_id") == invoice_id:
            ctx.update("xp_b_fees", f["id"], invoice_id=None)
    history(ctx, "invoice_void", r)
    return {"invoice_id": invoice_id, "status": "void"}


@operation(
    "issue_fee_invoice",
    "Issue an invoice from selected fee_ids for the registered company name/address in profile_id. Only provider fees are eligible, refunds reduce each fee, and active fee occupancy prevents duplication. The issue fee is charged even if the customer's broader period is incomplete.",
    {"profile_id": S, "fee_ids": A},
)
def issue_invoice(ctx, profile_id, fee_ids):
    p = ctx.record("xp_b_invoice_profiles", profile_id)
    ctx.require(len(fee_ids) > 0 and len(fee_ids) == len(set(fee_ids)), "FEE_SELECTION")
    fs = [ctx.record("xp_b_fees", x) for x in fee_ids]
    for f in fs:
        ctx.require(
            f["company"] == p["company"]
            and f["provider"] == "bank"
            and (f.get("invoice_id") is None),
            "FEE_NOT_INVOICEABLE",
        )
    amount = sum((f["amount_minor"] - f.get("refunded_minor", 0) for f in fs))
    ctx.require(amount > 0, "ZERO_INVOICE")
    debit(ctx, p, p["issue_fee_minor"], "invoice_issue_fee")
    r = ctx.create(
        "xp_b_invoices",
        customer_id=p["customer_id"],
        account_id=p["account_id"],
        currency=p["currency"],
        company=p["company"],
        address=p["address"],
        fee_ids=list(fee_ids),
        amount_minor=amount,
        status="issued",
        void_fee_minor=p["void_fee_minor"],
    )
    for f in fs:
        ctx.update("xp_b_fees", f["id"], invoice_id=r["id"])
    history(ctx, "invoice_issued", r, amount_minor=amount, fee_ids=list(fee_ids))
    return r


@operation(
    "deliver_fee_invoice",
    "Deliver an active invoice to the customer. Delivery freezes the disclosed amount; later refund corrections require voiding and replacement.",
    {"invoice_id": S},
)
def deliver_invoice(ctx, invoice_id):
    r = ctx.record("xp_b_invoices", invoice_id)
    ctx.require(r["status"] == "issued", "INVOICE_NOT_ISSUED")
    history(ctx, "invoice_delivered", r, amount_minor=r["amount_minor"], fee_ids=r["fee_ids"])
    return ctx.update("xp_b_invoices", invoice_id, status="delivered")


@operation(
    "amend_purchase_order",
    "Apply the supplier's published amendment to an unpaid order. This changes revision and value; existing protection quotes remain tied to their original revision.",
    {"order_id": S},
)
def amend_purchase(ctx, order_id):
    r = ctx.record("xp_b_orders", order_id)
    ctx.require(r["status"] == "unpaid" and r.get("amendment"), "NO_AMENDMENT")
    a = r["amendment"]
    return ctx.update(
        "xp_b_orders",
        order_id,
        amount_minor=a["amount_minor"],
        revision=a["revision"],
        amendment=None,
    )


@operation(
    "quote_purchase_protection",
    "Quote delivery protection for the current unpaid order revision. plan must be a key in the order's published plans, such as basic, standard or express, with fee and protection_limit_minor from that plan. This creates a quote and does not pay.",
    {"order_id": S, "plan": S},
)
def quote_protection(ctx, order_id, plan):
    r = ctx.record("xp_b_orders", order_id)
    ctx.require(plan in r["plans"] and r["status"] == "unpaid", "PLAN")
    return ctx.create(
        "xp_b_protection_quotes",
        customer_id=r["customer_id"],
        order_id=order_id,
        revision=r["revision"],
        plan=plan,
        **r["plans"][plan],
    )


@operation(
    "pay_purchase_order",
    "Pay an unpaid purchase order on direct or protected channel. direct has zero protection; a quote_id may cover only its quoted revision and limit. Stale quotes remain purchasable but confer zero cover on a changed order. Fees are irreversible.",
    {"order_id": S, "channel": S, "quote_id": S},
)
def pay_purchase(ctx, order_id, channel, quote_id):
    r = ctx.record("xp_b_orders", order_id)
    ctx.require(r["status"] == "unpaid" and channel in ("direct", "protected"), "ORDER_OR_CHANNEL")
    fee = 0
    cover = 0
    if channel == "protected":
        q = ctx.record("xp_b_protection_quotes", quote_id)
        ctx.require(q["order_id"] == order_id, "QUOTE_ORDER")
        fee = q["fee_minor"]
        cover = q["protection_limit_minor"] if q["revision"] == r["revision"] else 0
    debit(ctx, r, r["amount_minor"] + fee, "purchase")
    history(
        ctx, "purchase_paid", r, amount_minor=r["amount_minor"], fee_minor=fee, cover_minor=cover
    )
    return ctx.update(
        "xp_b_orders",
        order_id,
        status="paid",
        channel=channel,
        cover_minor=cover,
        paid_revision=r["revision"],
        fee_minor=fee,
    )


@operation(
    "pay_service_merchant",
    "Transfer amount_minor from source account in USD to merchant_id with subaccount_ref. Payment is irreversible. Merchants share display names; merchant legal entity, supported service and subaccount directory determine whether later application is possible.",
    {"merchant_id": S, "account_id": S, "subaccount_ref": S, "amount_minor": I},
)
def pay_service(ctx, merchant_id, account_id, subaccount_ref, amount_minor):
    m = ctx.record("xp_b_merchants", merchant_id)
    a = ctx.record("xp_accounts", account_id)
    ctx.require(amount_minor > 0, "AMOUNT")
    ctx.ledger(account_id, -amount_minor, "USD", "service_transfer", merchant_id)
    return ctx.create(
        "xp_b_service_receipts",
        customer_id=a["customer_id"],
        merchant_id=merchant_id,
        subaccount_ref=subaccount_ref,
        amount_minor=amount_minor,
        status="received",
    )


@operation(
    "apply_service_credit",
    "Apply a received merchant receipt to business_account_id after exact merchant and subaccount matching. A published merchant hold_minor is retained, so credited usable balance may be less than the transfer.",
    {"receipt_id": S, "business_account_id": S},
)
def apply_service(ctx, receipt_id, business_account_id):
    r = ctx.record("xp_b_service_receipts", receipt_id)
    b = ctx.record("xp_b_business_accounts", business_account_id)
    m = ctx.record("xp_b_merchants", r["merchant_id"])
    ctx.require(
        r["status"] == "received"
        and r["merchant_id"] == b["merchant_id"]
        and (r["subaccount_ref"] == b["subaccount_ref"]),
        "MERCHANT_SUBACCOUNT_MISMATCH",
    )
    held = b.get("held_minor", m["hold_minor"] if b.get("hold_applied") else 0)
    hold = min(r["amount_minor"], max(0, m["hold_minor"] - held))
    credit = r["amount_minor"] - hold
    ctx.update(
        "xp_b_service_receipts",
        receipt_id,
        status="applied",
        business_account_id=business_account_id,
        usable_minor=credit,
        held_minor=hold,
    )
    return ctx.update(
        "xp_b_business_accounts",
        business_account_id,
        usable_minor=b["usable_minor"] + credit,
        held_minor=held + hold,
        hold_applied=held + hold >= m["hold_minor"],
    )


@operation(
    "repair_gateway_mapping",
    "Set a gateway route's merchant account mapping. This affects future receipt delivery only; it neither charges a payer nor changes existing merchant orders.",
    {"route_id": S, "merchant_account": S},
)
def repair_gateway(ctx, route_id, merchant_account):
    r = ctx.record("xp_b_gateway_routes", route_id)
    ctx.require(merchant_account in r["allowed_accounts"], "UNSUPPORTED_MERCHANT")
    return ctx.update("xp_b_gateway_routes", route_id, merchant_account=merchant_account)


@operation(
    "replay_payment_receipt",
    "Replay the authentic settled gateway receipt through its current route. Delivery updates only the matching merchant order and is idempotent by gateway payment ID. Wrong mapping may update another merchant order and cannot be recalled.",
    {"payment_id": S},
)
def replay_receipt(ctx, payment_id):
    p = ctx.record("xp_b_gateway_payments", payment_id)
    r = ctx.record("xp_b_gateway_routes", p["route_id"])
    ctx.require(p["status"] == "settled", "NOT_SETTLED")
    candidates = [
        o
        for o in ctx.records("xp_b_store_orders")
        if o["merchant_account"] == r["merchant_account"]
        and o["external_reference"] == p["external_reference"]
    ]
    ctx.require(len(candidates) == 1, "ORDER_MAPPING")
    o = candidates[0]
    if o.get("receipt_payment_id") == payment_id:
        return o
    ids = o.get("receipt_payment_ids", [])
    if payment_id in ids:
        return o
    total = o.get("net_received_minor", 0) + p["amount_minor"] * (
        1 if p.get("receipt_kind", "capture") == "capture" else -1
    )
    history(
        ctx,
        "gateway_delivery",
        p,
        order_id=o["id"],
        merchant_account=r["merchant_account"],
        receipt_kind=p.get("receipt_kind", "capture"),
        amount_minor=p["amount_minor"],
    )
    return ctx.update(
        "xp_b_store_orders",
        o["id"],
        status="paid" if total == o["amount_minor"] else "reconciling",
        receipt_payment_id=payment_id,
        receipt_payment_ids=ids + [payment_id],
        net_received_minor=total,
    )


@operation(
    "charge_store_order",
    "Create a new card charge for a merchant order. This bank-legal operation charges again even if a gateway payment already exists; it is not a receipt-repair operation.",
    {"order_id": S},
)
def charge_store(ctx, order_id):
    o = ctx.record("xp_b_store_orders", order_id)
    ctx.require(o["status"] in ("unpaid", "reconciling"), "ORDER_PAID")
    debit(ctx, o, o["amount_minor"], "new_store_charge")
    history(ctx, "store_recharged", o, amount_minor=o["amount_minor"])
    return ctx.update("xp_b_store_orders", order_id, status="paid", receipt_payment_id="new-charge")


@operation(
    "set_platform_payout_rule",
    "Request a platform automatic payout rule enabled or disabled. A queued platform acknowledgement makes the change effective after delay_minutes. Already accepted payouts remain obligations.",
    {"rule_id": S, "enabled": B},
)
def set_payout(ctx, rule_id, enabled):
    r = ctx.record("xp_b_payout_rules", rule_id)
    version = r.get("change_version", 0) + 1
    ctx.update(
        "xp_b_payout_rules",
        rule_id,
        pending_enabled=enabled,
        change_version=version,
        pending_change_version=version,
    )
    ctx.schedule(
        "b_payout_rule",
        ctx.minute + r["delay_minutes"],
        rule_id,
        _enabled=enabled,
        _version=version,
    )
    return {"rule_id": rule_id, "status": "awaiting_platform_ack", "change_version": version}


@operation(
    "set_receiving_account_status",
    "Set a receiving account active or disabled. Disabling blocks manual and automatic receipts without cancelling any platform instruction.",
    {"receiving_id": S, "status": S},
)
def receiving_status(ctx, receiving_id, status):
    r = ctx.record("xp_b_receiving_accounts", receiving_id)
    ctx.require(status in ("active", "disabled"), "STATUS")
    return ctx.update("xp_b_receiving_accounts", receiving_id, status=status)


@operation(
    "resolve_accepted_payout",
    "Settle or return a platform payout already accepted before the automatic-rule acknowledgement. settle needs an active receiving account; return forfeits its published nonrefundable return fee.",
    {"payout_id": S, "disposition": S},
)
def accepted_payout(ctx, payout_id, disposition):
    r = ctx.record("xp_b_platform_payouts", payout_id)
    a = ctx.record("xp_b_receiving_accounts", r["receiving_id"])
    ctx.require(r["status"] == "accepted" and disposition in ("settle", "return"), "PAYOUT_STATE")
    if disposition == "settle":
        ctx.require(a["status"] == "active", "RECEIVING_DISABLED")
        ctx.ledger(r["account_id"], r["amount_minor"], r["currency"], "accepted_payout", payout_id)
    else:
        debit(ctx, r, r["return_fee_minor"], "payout_return_fee")
    return ctx.update(
        "xp_b_platform_payouts",
        payout_id,
        status="settled" if disposition == "settle" else "returned",
    )


@operation(
    "request_fx_order_cancellation",
    "Request cancellation of a specified-rate order. A fill may occur before the cancellation acknowledgement; optimized orders are noncancellable. Read confirmed filled_minor after acknowledgement before replacing.",
    {"order_id": S},
)
def cancel_fx(ctx, order_id):
    r = ctx.record("xp_b_fx_orders", order_id)
    ctx.require(r["product"] == "specified" and r["status"] == "open", "ORDER_NOT_CANCELLABLE")
    ctx.update("xp_b_fx_orders", order_id, status="cancel_pending")
    ctx.schedule("b_fx_cancel", ctx.minute + r["cancel_delay"], order_id)
    return {"order_id": order_id, "status": "cancel_pending", "filled_minor": r["filled_minor"]}


@operation(
    "buy_foreign_currency",
    "Buy target amount_minor of foreign currency using the published quote in requirement_id. Debit amount*rate_num//rate_den plus fee_minor in source currency. The purchase is irreversible; its source may be spot or replacement for audit.",
    {"requirement_id": S, "amount_minor": I, "source": S},
)
def buy_fx(ctx, requirement_id, amount_minor, source):
    r = ctx.record("xp_b_fx_requirements", requirement_id)
    ctx.require(amount_minor > 0 and source in ("spot", "replacement"), "BUY_PARAMETERS")
    cost = amount_minor * r["rate_num"] // r["rate_den"] + r["fee_minor"]
    debit(ctx, r, cost, "fx_purchase")
    ctx.update(
        "xp_b_fx_requirements", requirement_id, acquired_minor=r["acquired_minor"] + amount_minor
    )
    return history(ctx, "fx_bought", r, amount_minor=amount_minor, cost_minor=cost, source=source)


@operation(
    "pay_foreign_obligation",
    "Pay the exact unpaid foreign obligation from acquired foreign currency. Any excess acquired balance remains a real exposure and is not automatically reversed.",
    {"requirement_id": S},
)
def pay_fx(ctx, requirement_id):
    r = ctx.record("xp_b_fx_requirements", requirement_id)
    ctx.require(not r["paid"] and r["acquired_minor"] >= r["need_minor"], "FOREIGN_BALANCE")
    return ctx.update(
        "xp_b_fx_requirements",
        requirement_id,
        paid=True,
        paid_target_minor=r["need_minor"],
        remaining_minor=r["acquired_minor"] - r["need_minor"],
    )


@operation(
    "convert_shared_proceeds",
    "Convert the shared proceeds pool once using floor(source_minor*rate_num/rate_den). Exact rational weights and residual_rank in allocations govern the largest-remainder allocation; no money is assigned by this conversion alone.",
    {"pool_id": S},
)
def convert_pool(ctx, pool_id):
    r = ctx.record("xp_b_pools", pool_id)
    ctx.require(r["status"] == "unconverted", "ALREADY_CONVERTED")
    debit(ctx, r, r["source_minor"], "shared_conversion")
    return ctx.update(
        "xp_b_pools",
        pool_id,
        status="converted",
        target_total_minor=r["source_minor"] * r["rate_num"] // r["rate_den"],
        remaining_minor=r["source_minor"] * r["rate_num"] // r["rate_den"],
    )


@operation(
    "disburse_allocation",
    "Pay amount_minor from a converted pool to allocation_id. Bank permits any positive amount within remaining cash; contractual largest-remainder rights must be calculated separately. Transfers are irreversible.",
    {"allocation_id": S, "amount_minor": I},
)
def disburse(ctx, allocation_id, amount_minor):
    a = ctx.record("xp_b_allocations", allocation_id)
    p = ctx.record("xp_b_pools", a["pool_id"])
    ctx.require(
        p["status"] == "converted" and 0 < amount_minor <= p["remaining_minor"], "POOL_BALANCE"
    )
    ctx.update("xp_b_pools", p["id"], remaining_minor=p["remaining_minor"] - amount_minor)
    ctx.update("xp_b_allocations", allocation_id, paid_minor=a["paid_minor"] + amount_minor)
    return history(ctx, "allocation_paid", a, amount_minor=amount_minor)


@operation(
    "prepare_currency_leg",
    "Prepare the named settlement pair's source or counterparty leg. Counterparty readiness comes from its published bank receipt; preparation charges prepare_fee_minor once per attempt, even if unavailable.",
    {"pair_id": S, "leg": S},
)
def prepare_leg(ctx, pair_id, leg):
    r = ctx.record("xp_b_settlement_pairs", pair_id)
    ctx.require(leg in ("source", "counterparty"), "LEG")
    key = leg + "_prepared"
    ctx.require(not r.get(key), "ALREADY_PREPARED")
    debit(ctx, r, r["prepare_fee_minor"], "settlement_prepare_fee")
    ready = True if leg == "source" else r["counterparty_ready"]
    return ctx.update(
        "xp_b_settlement_pairs",
        pair_id,
        **{key: ready, "last_prepare_result": "ready" if ready else "unavailable"},
    )


@operation(
    "settle_currency_pair",
    "Settle both currency legs synchronously, requiring both prepared, or release source principal unilaterally. unilateral is legal but creates irreversible principal exposure if the counterparty is unavailable. Fees/principal are published on the pair.",
    {"pair_id": S, "mode": S},
)
def settle_pair(ctx, pair_id, mode):
    r = ctx.record("xp_b_settlement_pairs", pair_id)
    ctx.require(r["status"] == "open" and mode in ("synchronous", "unilateral"), "PAIR_STATE")
    if mode == "synchronous":
        ctx.require(r["source_prepared"] and r["counterparty_prepared"], "LEGS_NOT_READY")
    fee = r["sync_fee_minor"] if mode == "synchronous" else 0
    debit(ctx, r, r["source_minor"] + fee, "currency_principal")
    received = r["target_minor"] if r["counterparty_ready"] else 0
    return ctx.update(
        "xp_b_settlement_pairs",
        pair_id,
        status="settled",
        mode=mode,
        received_minor=received,
        exposure_minor=0 if mode == "synchronous" else r["source_minor"],
    )


@operation(
    "release_currency_preparations",
    "Release prepared legs on an unsettled pair. Preparation fees are not refunded. Releasing leaves the source principal available for another pair.",
    {"pair_id": S},
)
def release_pair(ctx, pair_id):
    r = ctx.record("xp_b_settlement_pairs", pair_id)
    ctx.require(r["status"] == "open", "PAIR_SETTLED")
    return ctx.update(
        "xp_b_settlement_pairs",
        pair_id,
        source_prepared=False,
        counterparty_prepared=False,
        status="released",
    )


@operation(
    "request_fee_correction",
    "Claim a service-fee correction for fee_id and amount_minor. Only bank-provider overcharges under the published billing record are credited; third-party charges and missed optional coupons are ineligible. Review can legally reject a claim and charges the published review fee.",
    {"fee_id": S, "amount_minor": I},
)
def fee_correction(ctx, fee_id, amount_minor):
    r = ctx.record("xp_b_billing", fee_id)
    ctx.require(not r.get("reviewed"), "ALREADY_REVIEWED")
    debit(ctx, r, r["review_fee_minor"], "billing_review_fee")
    rate = (
        r["high_rate_bps"]
        if r["eligible_volume_minor"] < r["tier_threshold_minor"]
        else r["low_rate_bps"]
    )
    proper = r["base_minor"] * rate // 10000 if r["provider"] == "bank" else r["charged_minor"]
    eligible = max(0, r["charged_minor"] - proper) if not r["missed_coupon"] else 0
    refund = amount_minor if 0 < amount_minor <= eligible else 0
    if refund:
        ctx.ledger(r["account_id"], refund, r["currency"], "billing_credit", fee_id)
    return ctx.update(
        "xp_b_billing",
        fee_id,
        reviewed=True,
        refund_minor=refund,
        review_status="accepted" if refund else "rejected",
        proper_minor=proper,
    )


@operation(
    "submit_tax_agent_application",
    "Submit customer-uploaded tax-agent appointment for agent_id; approval is published by a bank event after review_delay. Approved agency is distinct from configured payment routing.",
    {"agent_id": S},
)
def apply_tax_agent(ctx, agent_id):
    r = ctx.record("xp_b_tax_agents", agent_id)
    ctx.require(r["status"] == "new", "AGENT_APPLICATION_STATE")
    require_document(
        ctx,
        agent_id,
        "tax_agent_appointment",
        lambda d: d.get("company") == r["principal_company"]
        and d.get("agent") == r["legal_name"]
        and (d.get("jurisdiction") in r["jurisdictions"])
        and (d.get("signed") is True),
    )
    ctx.update("xp_b_tax_agents", agent_id, status="reviewing")
    ctx.schedule("b_tax_agent", ctx.minute + r["review_delay"], agent_id)
    return {"agent_id": agent_id, "status": "reviewing"}


@operation(
    "configure_tax_agent_route",
    "Enable an approved agent for its published jurisdictions. This creates a distinct route; historical recipient identity cannot be overwritten.",
    {"agent_id": S},
)
def configure_agent(ctx, agent_id):
    r = ctx.record("xp_b_tax_agents", agent_id)
    ctx.require(r["status"] == "approved", "AGENT_NOT_APPROVED")
    return ctx.update("xp_b_tax_agents", agent_id, configured=True)


@operation(
    "pay_tax_obligation",
    "Pay the full unpaid tax item to recipient_id. The payment network accepts any listed active recipient; product/jurisdiction eligibility and agent appointment must be checked by the caller. Unqualified routing may post cash without discharging the tax obligation.",
    {"obligation_id": S, "recipient_id": S},
)
def pay_tax(ctx, obligation_id, recipient_id):
    r = ctx.record("xp_b_tax_obligations", obligation_id)
    p = ctx.record("xp_b_tax_agents", recipient_id)
    ctx.require(not r["paid"] and p["status"] in ("approved", "active"), "TAX_PAYMENT_STATE")
    debit(ctx, r, r["amount_minor"] + p["fee_minor"], "tax_payment")
    valid = r["jurisdiction"] in p["jurisdictions"] and (
        p["kind"] == "authority" or p["configured"]
    )
    return ctx.update(
        "xp_b_tax_obligations",
        obligation_id,
        paid=True,
        recipient_id=recipient_id,
        discharged=valid,
        paid_minute=ctx.minute,
        fee_minor=p["fee_minor"],
    )


@operation(
    "request_tax_overpayment_review",
    "Ask the tax authority to review the referenced original payment, with a customer-uploaded overpayment statement. The authority's fixed event determines approved credit, not the bank or the user.",
    {"claim_id": S},
)
def review_overpay(ctx, claim_id):
    r = ctx.record("xp_b_tax_claims", claim_id)
    ctx.require(r["status"] == "new", "CLAIM_APPLICATION_STATE")
    require_document(
        ctx,
        claim_id,
        "overpayment_statement",
        lambda d: d.get("company") == r["company"]
        and d.get("period") == r["period"]
        and (d.get("paid_minor") == r["original_paid_minor"])
        and (d.get("asserted_excess_minor") == r["claimed_minor"]),
    )
    ctx.update("xp_b_tax_claims", claim_id, status="reviewing")
    ctx.schedule("b_tax_credit", ctx.minute + r["review_delay"], claim_id)
    return {"claim_id": claim_id, "status": "reviewing"}


@operation(
    "dispose_tax_credit",
    "Use an authority-approved credit by offsetting the next period or requesting a cash refund. Cash refund is irreversible and arrives only after refund_delay; offset immediately reduces the named next liability. amount_minor cannot exceed approved unused credit.",
    {"claim_id": S, "mode": S, "amount_minor": I},
)
def dispose_credit(ctx, claim_id, mode, amount_minor):
    r = ctx.record("xp_b_tax_claims", claim_id)
    ctx.require(
        r["status"] == "approved"
        and mode in ("offset", "cash")
        and (0 < amount_minor <= r["available_minor"]),
        "CREDIT_UNAVAILABLE",
    )
    ctx.update(
        "xp_b_tax_claims",
        claim_id,
        available_minor=r["available_minor"] - amount_minor,
        **{mode + "_minor": r.get(mode + "_minor", 0) + amount_minor},
    )
    if mode == "cash":
        ctx.schedule(
            "b_tax_cash", ctx.minute + r["refund_delay"], claim_id, _amount_minor=amount_minor
        )
    else:
        o = ctx.record("xp_b_next_tax", r["next_id"])
        ctx.require(o["amount_minor"] - o["paid_minor"] >= amount_minor, "OFFSET_EXCESS")
        ctx.update("xp_b_next_tax", o["id"], offset_minor=o["offset_minor"] + amount_minor)
    return ctx.record("xp_b_tax_claims", claim_id)


@operation(
    "pay_next_tax_period",
    "Pay the remaining next-period liability in cash after approved offsets. Cash debit cannot use an unreceived requested refund.",
    {"period_id": S},
)
def next_tax(ctx, period_id):
    r = ctx.record("xp_b_next_tax", period_id)
    amount = r["amount_minor"] - r["offset_minor"] - r["paid_minor"]
    ctx.require(amount > 0, "NO_TAX_DUE")
    debit(ctx, r, amount, "next_tax")
    return ctx.update(
        "xp_b_next_tax", period_id, paid_minor=r["paid_minor"] + amount, paid_minute=ctx.minute
    )


@operation(
    "register_customs_refund_account",
    "Register a synthetic customs refund ACH destination for importer_id. The destination must belong to the importer and support ACH; registration creates no refund entitlement.",
    {"importer_id": S, "destination_id": S},
)
def register_customs(ctx, importer_id, destination_id):
    r = ctx.record("xp_b_importers", importer_id)
    d = ctx.record("xp_b_destinations", destination_id)
    ctx.require(d["owner"] == r["legal_name"] and d.get("ach"), "IMPORTER_ACH_REQUIRED")
    return ctx.update("xp_b_importers", importer_id, destination_id=destination_id)


@operation(
    "submit_customs_refund_claim",
    "Submit distinct entry_ids for the registered importer after uploading the customs schedule. Duplicate IDs are rejected before any filing fee or application. Review excludes ineligible entries according to fixed authority evidence and charges filing_fee_minor, even if the submission is incomplete.",
    {"importer_id": S, "entry_ids": A},
)
def customs_claim(ctx, importer_id, entry_ids):
    r = ctx.record("xp_b_importers", importer_id)
    ctx.require(r.get("destination_id") and (not r.get("claim_submitted")), "CUSTOMS_PREREQUISITES")
    ctx.require(len(entry_ids) == len(set(entry_ids)), "DUPLICATE_CUSTOMS_ENTRY")
    require_document(
        ctx,
        importer_id,
        "customs_schedule",
        lambda d: d.get("importer") == r["legal_name"]
        and set(entry_ids) <= set(d.get("entries", [])),
    )
    es = [ctx.record("xp_b_customs_entries", x) for x in entry_ids]
    ctx.require(es and all((x["importer_id"] == importer_id for x in es)), "ENTRY_OWNERSHIP")
    debit(ctx, r, r["filing_fee_minor"], "customs_filing_fee")
    ctx.update("xp_b_importers", importer_id, claim_submitted=True, submitted_entry_ids=entry_ids)
    ctx.schedule("b_customs_review", ctx.minute + r["review_delay"], importer_id)
    return {"importer_id": importer_id, "status": "reviewing"}


@operation(
    "accept_customs_award",
    "Accept the authority's reviewed customs award into the registered ACH account, or withdraw it. amount_minor must exactly match the published award in USD minor units. Acceptance transfers only that reviewed total; withdrawal forfeits this claim.",
    {"importer_id": S, "decision": S, "amount_minor": I},
)
def accept_customs(ctx, importer_id, decision, amount_minor):
    r = ctx.record("xp_b_importers", importer_id)
    ctx.require(
        r.get("award_status") == "reviewed" and decision in ("accept", "withdraw"), "AWARD_STATE"
    )
    ctx.require(amount_minor == r["award_minor"], "AWARD_AMOUNT_MISMATCH")
    if decision == "accept":
        d = ctx.record("xp_b_destinations", r["destination_id"])
        ctx.update(
            "xp_b_destinations",
            d["id"],
            received_minor=d.get("received_minor", 0) + r["award_minor"],
        )
    return ctx.update(
        "xp_b_importers",
        importer_id,
        award_status="received" if decision == "accept" else "withdrawn",
    )


@operation(
    "create_bank_evidence",
    "Create a signed receipt, signed statement or account certificate for evidence_request_id. field_names selects disclosed fields; statement includes all transactions, receipt only the requested transaction, and certificate proves ownership only. Format must be pdf for a seal. Creation charges the public fee and does not deliver it.",
    {"evidence_request_id": S, "kind": S, "field_names": A, "format": S},
)
def create_evidence(ctx, evidence_request_id, kind, field_names, format):
    r = ctx.record("xp_b_evidence_requests", evidence_request_id)
    ctx.require(
        kind in ("receipt", "statement", "certificate") and format in ("pdf", "csv"),
        "EVIDENCE_KIND",
    )
    ctx.require(set(field_names) <= set(r["available_fields"]), "UNKNOWN_FIELD")
    debit(ctx, r, r["creation_fee_minor"], "evidence_creation_fee")
    return ctx.create(
        "xp_b_bank_evidence",
        customer_id=r["customer_id"],
        request_id=evidence_request_id,
        kind=kind,
        field_names=field_names,
        format=format,
        sealed=format == "pdf",
        transaction_ids=(
            r["all_transaction_ids"]
            if kind == "statement"
            else [r["transaction_id"]] if kind == "receipt" else []
        ),
        delivered=False,
    )


@operation(
    "deliver_bank_evidence",
    "Disclose generated evidence to the request's recipient. Delivery is irreversible; customer consent scope and recipient acceptance must be evaluated before calling.",
    {"evidence_id": S},
)
def deliver_evidence(ctx, evidence_id):
    r = ctx.record("xp_b_bank_evidence", evidence_id)
    ctx.require(not r["delivered"], "ALREADY_DELIVERED")
    return ctx.update(
        "xp_b_bank_evidence", evidence_id, delivered=True, delivered_minute=ctx.minute
    )


@operation(
    "recover_access_channel",
    "Recover login, contact, or payment credential for profile_id using an actual identity_recovery upload. A review event activates only the selected channel after review_delay and increments its credential version; other channels are unaffected.",
    {"profile_id": S, "channel": S},
)
def recover_channel(ctx, profile_id, channel):
    r = ctx.record("xp_b_access_profiles", profile_id)
    ctx.require(channel in ("login", "contact", "payment"), "RECOVERY_CHANNEL")
    require_document(
        ctx,
        profile_id,
        "identity_recovery",
        lambda d: d.get("identity") == r["holder_name"]
        and d.get("reachable_channel") == "registered email"
        and (d.get("signed") is True)
        and (r["email_status"] == "active"),
    )
    debit(ctx, r, r["recovery_fee_minor"], "credential_recovery_fee")
    ctx.schedule("b_access_recovery", ctx.minute + r["review_delay"], profile_id, _channel=channel)
    return {"profile_id": profile_id, "channel": channel, "status": "reviewing"}


@operation(
    "create_payment_challenge",
    "Create a payment authentication challenge bound immutably to the current profile credential version and obligation amount. method is registered_email or mobile. The corresponding contact channel must be active. challenge_fee_minor is charged even if later credential recovery makes this challenge stale; expires after challenge_ttl minutes.",
    {"profile_id": S, "obligation_id": S, "method": S},
)
def create_challenge(ctx, profile_id, obligation_id, method):
    p = ctx.record("xp_b_access_profiles", profile_id)
    r = ctx.record("xp_b_secure_obligations", obligation_id)
    ctx.require(
        method in ("registered_email", "mobile")
        and (
            p["email_status"] == "active"
            if method == "registered_email"
            else p["contact_status"] == "active"
        ),
        "AUTHENTICATION_CHANNEL_UNAVAILABLE",
    )
    debit(ctx, p, p["challenge_fee_minor"], "authentication_challenge_fee")
    return ctx.create(
        "xp_b_payment_challenges",
        customer_id=p["customer_id"],
        profile_id=profile_id,
        obligation_id=obligation_id,
        credential_version=p["payment_version"],
        amount_minor=r["amount_minor"],
        method=method,
        expires_minute=ctx.minute + p["challenge_ttl"],
        status="open",
    )


@operation(
    "execute_authenticated_payment",
    "Execute an unpaid obligation using the active credential_version and matching unexpired challenge_id. The challenge binds obligation, amount and credential version; recovery invalidates old-version challenges. Login/contact reset alone does not activate payment authentication.",
    {"obligation_id": S, "profile_id": S, "credential_version": I, "challenge_id": S},
)
def authenticated_payment(ctx, obligation_id, profile_id, credential_version, challenge_id):
    p = ctx.record("xp_b_access_profiles", profile_id)
    r = ctx.record("xp_b_secure_obligations", obligation_id)
    challenge = ctx.record("xp_b_payment_challenges", challenge_id)
    ctx.require(
        p["payment_status"] == "active"
        and p["payment_version"] == credential_version
        and (not r["paid"]),
        "PAYMENT_AUTHENTICATION",
    )
    ctx.require(
        challenge["status"] == "open"
        and challenge["profile_id"] == profile_id
        and (challenge["obligation_id"] == obligation_id)
        and (challenge["amount_minor"] == r["amount_minor"])
        and (challenge["credential_version"] == credential_version)
        and (ctx.minute <= challenge["expires_minute"]),
        "STALE_PAYMENT_CHALLENGE",
    )
    ctx.require(
        (
            p["email_status"] == "active"
            if challenge["method"] == "registered_email"
            else p["contact_status"] == "active"
        ),
        "AUTHENTICATION_CHANNEL_UNAVAILABLE",
    )
    debit(ctx, r, r["amount_minor"], "authenticated_payment")
    history(
        ctx,
        "authenticated_payment",
        r,
        profile_id=profile_id,
        credential_version=credential_version,
        challenge_id=challenge_id,
    )
    ctx.update("xp_b_payment_challenges", challenge_id, status="consumed", used_minute=ctx.minute)
    return ctx.update(
        "xp_b_secure_obligations",
        obligation_id,
        paid=True,
        paid_minute=ctx.minute,
        credential_version=credential_version,
        challenge_id=challenge_id,
    )


@operation(
    "revoke_access_credential",
    "Revoke one credential or all credentials in the same family. Accepted transfer instructions remain separately actionable; revocation blocks future queued submissions but does not recall accepted transfers.",
    {"credential_id": S, "scope": S},
)
def revoke_credential(ctx, credential_id, scope):
    r = ctx.record("xp_b_credentials", credential_id)
    ctx.require(scope in ("one", "family"), "REVOCATION_SCOPE")
    targets = (
        [r]
        if scope == "one"
        else [x for x in ctx.records("xp_b_credentials") if x["family"] == r["family"]]
    )
    for t in targets:
        ctx.update("xp_b_credentials", t["id"], status="revoked")
    return {"revoked_ids": [x["id"] for x in targets]}


@operation(
    "resolve_queued_transfer",
    "Cancel or execute an accepted/queued transfer. Cancellation charges its published cancellation fee. Executing queued transfers requires an active credential; already accepted transfers can execute even after credential revocation. A legal transfer can still be unauthorized by the customer.",
    {"transfer_id": S, "action": S},
)
def resolve_transfer(ctx, transfer_id, action):
    r = ctx.record("xp_b_security_transfers", transfer_id)
    ctx.require(
        r["status"] in ("queued", "accepted") and action in ("cancel", "execute"), "TRANSFER_STATE"
    )
    if action == "cancel":
        debit(ctx, r, r["cancel_fee_minor"], "security_cancel_fee")
        status = "cancelled"
    else:
        c = ctx.record("xp_b_credentials", r["credential_id"])
        ctx.require(r["status"] == "accepted" or c["status"] == "active", "CREDENTIAL_REVOKED")
        debit(ctx, r, r["amount_minor"], "security_transfer")
        status = "executed"
    return ctx.update(
        "xp_b_security_transfers", transfer_id, status=status, resolved_minute=ctx.minute
    )


@operation(
    "submit_delegate_scope_application",
    "Submit an actual delegation_letter to enlarge a delegate grant. Fixed review result adds the published requested_permissions after review_delay; no early disclosure is authorized.",
    {"grant_id": S},
)
def delegate_apply(ctx, grant_id):
    r = ctx.record("xp_b_delegate_grants", grant_id)
    ctx.require(r["status"] == "pending", "DELEGATION_STATE")
    require_document(
        ctx,
        grant_id,
        "delegation_letter",
        lambda d: d.get("principal") == r["principal_name"]
        and d.get("delegate") == r["delegate_name"]
        and (set(d.get("permissions", [])) == set(r["requested_permissions"]))
        and (d.get("signed") is True),
    )
    ctx.schedule("b_delegate_review", ctx.minute + r["review_delay"], grant_id)
    return {"grant_id": grant_id, "status": "reviewing"}


@operation(
    "deliver_delegate_data",
    "Deliver selected field_names for business_id to a contact using grant_id. Bank delivery service can disclose any fields in its report catalog; the caller must enforce company-specific current delegation and customer scope. Each disclosure is immutable.",
    {"grant_id": S, "business_id": S, "field_names": A},
)
def deliver_delegate(ctx, grant_id, business_id, field_names):
    g = ctx.record("xp_b_delegate_grants", grant_id)
    b = ctx.record("xp_b_managed_businesses", business_id)
    ctx.require(set(field_names) <= set(b["catalog_fields"]), "UNKNOWN_FIELD")
    return ctx.create(
        "xp_b_disclosures",
        customer_id=g["customer_id"],
        grant_id=grant_id,
        business_id=business_id,
        field_names=field_names,
        permissions_at_delivery=deepcopy(g["permissions"]),
        grant_business_id=g["business_id"],
        minute=ctx.minute,
    )


@operation(
    "execute_delegated_payment",
    "Pay the managed business obligation under a currently effective company-specific payment grant. Query or submit permission does not grant payment authority.",
    {"grant_id": S, "business_id": S},
)
def delegated_payment(ctx, grant_id, business_id):
    g = ctx.record("xp_b_delegate_grants", grant_id)
    b = ctx.record("xp_b_managed_businesses", business_id)
    ctx.require(
        g["business_id"] == business_id and "pay" in g["permissions"] and (not b["paid"]),
        "PAYMENT_DELEGATION",
    )
    debit(ctx, b, b["due_minor"], "delegated_payment")
    return ctx.update("xp_b_managed_businesses", business_id, paid=True, paid_minute=ctx.minute)


def apply_event(ctx, event):
    k = event["kind"]
    sid = event["subject_id"]
    payload = event.get("_payload", {})
    if k == "b_declaration":
        r = ctx.record("xp_b_export_lots", sid)
        if r.get("product") == "declaration":
            ctx.update(
                "xp_b_export_lots", sid, declaration_status="issued", declaration_id="DECL-" + sid
            )
    elif k == "b_platform_acceptance":
        rule = ctx.record("xp_b_payout_rules", sid)
        if rule["enabled"]:
            ctx.create(
                "xp_b_platform_payouts",
                customer_id=rule["customer_id"],
                account_id=payload["account_id"],
                currency="USD",
                receiving_id=rule["receiving_id"],
                amount_minor=payload["amount_minor"],
                return_fee_minor=80,
                status="accepted",
                source_event_id=event["id"],
            )
            ctx.update("xp_events", event["id"], disposition="accepted")
        else:
            ctx.update("xp_events", event["id"], disposition="suppressed_by_platform_rule")
    elif k == "b_billing_volume":
        r = ctx.record("xp_b_billing", sid)
        ctx.update(
            "xp_b_billing", sid, eligible_volume_minor=payload["volume_minor"], basis_status="final"
        )
    elif k == "b_fee_refund":
        r = ctx.record("xp_b_fees", sid)
        ctx.update(
            "xp_b_fees", sid, refunded_minor=r.get("refunded_minor", 0) + payload["_amount_minor"]
        )
    elif k == "b_payout_rule":
        r = ctx.record("xp_b_payout_rules", sid)
        version = payload["_version"]
        fields = dict(enabled=payload["_enabled"], ack_minute=ctx.minute, ack_version=version)
        if version == r.get("pending_change_version"):
            fields.update(pending_enabled=None, pending_change_version=None)
        ctx.update("xp_b_payout_rules", sid, **fields)
        ctx.update(
            "xp_events",
            event["id"],
            acknowledged_enabled=payload["_enabled"],
            acknowledged_version=version,
        )
    elif k == "b_fx_fill":
        r = ctx.record("xp_b_fx_orders", sid)
        if r["status"] in ("open", "cancel_pending"):
            n = payload["_amount_minor"]
            q = ctx.record("xp_b_fx_requirements", r["requirement_id"])
            cost = n * q["rate_num"] // q["rate_den"]
            if ctx.record("xp_accounts", q["account_id"])["balance_minor"] < cost:
                ctx.update(
                    "xp_b_fx_orders", sid, last_bank_result="fill_rejected_insufficient_source"
                )
                ctx.update(
                    "xp_events",
                    event["id"],
                    outcome="rejected_insufficient_source",
                    attempted_target_minor=n,
                )
                return True
            debit(ctx, q, cost, "conditional_fx_fill")
            ctx.update("xp_b_fx_orders", sid, filled_minor=r["filled_minor"] + n)
            ctx.update("xp_b_fx_requirements", q["id"], acquired_minor=q["acquired_minor"] + n)
    elif k == "b_fx_cancel":
        ctx.update("xp_b_fx_orders", sid, status="cancelled")
    elif k == "b_tax_agent":
        ctx.update("xp_b_tax_agents", sid, status="approved")
    elif k == "b_tax_credit":
        r = ctx.record("xp_b_tax_claims", sid)
        amount = r["_authority_review_minor"]
        ctx.update(
            "xp_b_tax_claims", sid, status="approved", approved_minor=amount, available_minor=amount
        )
    elif k == "b_tax_cash":
        r = ctx.record("xp_b_tax_claims", sid)
        ctx.ledger(
            r["account_id"], payload["_amount_minor"], r["currency"], "authority_refund", sid
        )
        ctx.update(
            "xp_b_tax_claims",
            sid,
            cash_received_minor=r.get("cash_received_minor", 0) + payload["_amount_minor"],
        )
    elif k == "b_customs_review":
        r = ctx.record("xp_b_importers", sid)
        es = [ctx.record("xp_b_customs_entries", i) for i in r["submitted_entry_ids"]]
        amount = sum((x["duty_minor"] for x in es if x["_eligible"]))
        for entry in es:
            ctx.update(
                "xp_b_customs_entries",
                entry["id"],
                eligible=entry["_eligible"],
                review_status="reviewed",
            )
        ctx.update(
            "xp_b_importers",
            sid,
            award_status="reviewed",
            award_minor=amount,
            excluded_entry_ids=[x["id"] for x in es if not x["_eligible"]],
        )
    elif k == "b_access_recovery":
        r = ctx.record("xp_b_access_profiles", sid)
        c = payload["_channel"]
        ctx.update(
            "xp_b_access_profiles",
            sid,
            **{c + "_status": "active", c + "_version": r[c + "_version"] + 1},
        )
    elif k == "b_delegate_review":
        r = ctx.record("xp_b_delegate_grants", sid)
        ctx.update(
            "xp_b_delegate_grants",
            sid,
            permissions=sorted(set(r["permissions"] + r["requested_permissions"])),
            status="active",
            effective_minute=ctx.minute,
        )
    else:
        return False
    return True


_EVIDENCE_BINDINGS = {
    38: ("submit_tax_agent_application", "agent_id", "tax_agent_appointment"),
    -39: ("request_tax_overpayment_review", "claim_id", "overpayment_statement"),
    39: ("submit_customs_refund_claim", "importer_id", "customs_schedule"),
    41: ("recover_access_channel", "profile_id", "identity_recovery"),
    -41: ("submit_delegate_scope_application", "grant_id", "delegation_letter"),
}
for _number, (_operation, _target_argument, _purpose) in _EVIDENCE_BINDINGS.items():
    _binding = f' Before executing {_operation}, the customer must call upload_file with target_id equal to the {_target_argument} used in that operation and purpose="{_purpose}". The assistant obtains that business record ID from bank records and supplies it in the upload instruction. A case ID or approval request ID is not the evidence target. Listing a file is not a submission. A matching upload is immediately available to execution; upload alone does not schedule processing, and the submission may be corrected before retrying the same approved request. DOCUMENT_SUBMISSION_REQUIRED means no submission matches both the required target and purpose. DOCUMENT_CONTENT_MISMATCH means matching submissions exist but their contents do not satisfy the published evidence requirements.'
    OPERATIONS[_operation]["description"] += _binding

from payment.runtime.products.psp.accounts import obligations as _enhancements

apply_event = _enhancements.register(OPERATIONS, apply_event)
from payment.runtime.products.psp.accounts import tax_and_authority as _diversity_revision

apply_event = _diversity_revision.register(OPERATIONS, apply_event)
from payment.runtime.products.psp.accounts import dependencies as _reasoning_revision

apply_event = _reasoning_revision.register(OPERATIONS, apply_event)
