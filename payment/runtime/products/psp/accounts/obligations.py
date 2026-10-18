"""Linked account obligations with shared approval and transaction rollback."""

from copy import deepcopy

S = {"type": "string"}
I = {"type": "integer", "minimum": 0}
A = {"type": "array", "items": S}
OPERATIONS = {}


def expose(name, table, action, handler, description, **properties):

    def execute(ctx, record_id, **arguments):
        return handler(ctx, ctx.record(table, record_id), action, **arguments)

    OPERATIONS[name] = {
        "description": description
        + " record_id identifies the named public business record. Amounts are integer USD minor units unless the record names a target currency.",
        "parameters": {
            "type": "object",
            "properties": {"record_id": S, **properties},
            "required": ["record_id", *properties],
            "additionalProperties": False,
        },
        "read_only": False,
        "function": execute,
    }


def update(ctx, r, **fields):
    return ctx.update(r["collection"], r["id"], **fields)


def charge(ctx, r, amount, kind):
    if amount:
        return ctx.ledger(r["account_id"], -amount, "USD", kind, r["id"])


def post(ctx, r, amount, kind):
    if amount:
        return ctx.ledger(r["account_id"], amount, "USD", kind, r["id"])


def event(ctx, r, label, fields, delay=1):
    return ctx.schedule(
        "b_enhance_" + label, ctx.minute + delay, r["id"], table=r["collection"], fields=fields
    )


def mark(ctx, r, kind, **fields):
    ctx.emit(kind, subject_id=r["id"], **fields)
    return update(ctx, r, **fields)


def one(ctx, table):
    rows = ctx.records(table)
    ctx.require(len(rows) == 1, "BUSINESS_RECORD_REQUIRED")
    return rows[0]


def procurement(ctx, r, action, choice=None):
    order = ctx.record("xp_b_orders", r["order_id"])
    if action == "terms":
        ctx.require(not r.get("terms_fixed") and choice in r["risk_terms"], "DELIVERY_TERMS")
        return mark(ctx, r, "delivery_contract_fixed", terms_fixed=True, risk_term=choice)
    if action == "inspect":
        ctx.require(
            order["status"] == "paid" and (not r.get("inspection_requested")),
            "PAID_ORDER_INSPECTION",
        )
        update(ctx, r, inspection_requested=True)
        event(
            ctx,
            r,
            "shipment",
            {
                "inspection_complete": True,
                "missing_units": r["_missing_units"],
                "inspection_order_revision": order["paid_revision"],
            },
        )
        return {"inspection": "pending", "order_id": order["id"]}
    if action == "remedy":
        ctx.require(
            r.get("inspection_complete") and (not r.get("remedy")) and (choice in r["remedies"]),
            "SHIPMENT_REMEDY",
        )
        charge(ctx, r, r["remedies"][choice]["fee_minor"], "shipment_remedy_fee")
        covered = (
            order.get("cover_minor", 0) >= order["amount_minor"]
            and r.get("risk_term") == "destination_inspection"
        )
        replacement = (
            r["missing_units"]
            if choice in ("manufacturer_replacement", "priority_replacement") and covered
            else 0
        )
        update(
            ctx,
            r,
            remedy=choice,
            claim_waived=choice == "carrier_cash_settlement",
            replacement_requested=replacement,
        )
        event(
            ctx,
            r,
            "replacement",
            {"replacement_received": replacement, "replacement_inspected": True},
            r["remedies"][choice]["delay"],
        )
        return {
            "replacement_units": replacement,
            "claim_waived": choice == "carrier_cash_settlement",
        }
    ctx.require(
        r.get("inspection_complete") and (not r["missing_units"] or r.get("replacement_inspected")),
        "REPLACEMENT_NOT_RECEIVED",
    )
    return mark(
        ctx,
        r,
        "procurement_delivery_accepted",
        delivery_accepted=True,
        accepted_units=r["ordered_units"] - r["missing_units"] + r.get("replacement_received", 0),
        rights_at_acceptance=r.get("risk_term"),
    )


expose(
    "fix_supplier_delivery_terms",
    "xp_b_delivery_contracts",
    "terms",
    procurement,
    "Fix risk terms before paying the linked purchase order. risk_terms describes claim retention or commercial waiver; payment records the selected contract.",
    choice=S,
)
expose(
    "inspect_paid_supplier_shipment",
    "xp_b_delivery_contracts",
    "inspect",
    procurement,
    "Request carrier inspection of the paid order's actual revision. The event publishes missing units; customer assurances do not replace this receipt.",
)
expose(
    "elect_supplier_shortage_remedy",
    "xp_b_delivery_contracts",
    "remedy",
    procurement,
    "Elect one remedy in remedies after inspection. Replacement requires retained claims and sufficient paid-order protection. A waiver or cash concession can legally forfeit undelivered goods.",
    choice=S,
)
expose(
    "accept_supplier_delivery_result",
    "xp_b_delivery_contracts",
    "accept",
    procurement,
    "Accept the original and replacement units actually received. Acceptance is immutable; it may leave a short delivery if claims were waived.",
)


def campaign(ctx, r, action, choice=None):
    wallet = ctx.record("xp_b_business_accounts", r["wallet_id"])
    if action == "reserve":
        ctx.require(
            not r.get("reservation") and wallet["usable_minor"] >= r["required_minor"],
            "CAMPAIGN_CREDIT_REQUIRED",
        )
        update(ctx, r, reservation=True, reserved_minor=r["required_minor"])
        event(
            ctx,
            r,
            "capacity",
            {"capacity_reviewed": True, "accepted_capacity_minor": r["_accepted_capacity_minor"]},
        )
        return {"reserved_minor": r["required_minor"], "allocation": "pending_provider"}
    if action == "release":
        ctx.require(
            r.get("capacity_reviewed") and (not r.get("released")), "CAPACITY_REVIEW_REQUIRED"
        )
        amount = r["required_minor"] - r["accepted_capacity_minor"]
        return mark(
            ctx,
            r,
            "unserved_campaign_capacity_released",
            released=True,
            free_capacity_minor=amount,
            reserved_minor=r["accepted_capacity_minor"],
        )
    if action == "makegood":
        ctx.require(
            r.get("released") and (not r.get("makegood")) and (choice in r["placements"]),
            "MAKEGOOD_SELECTION",
        )
        placement = r["placements"][choice]
        charge(ctx, r, placement["fee_minor"], "campaign_rebooking_fee")
        return mark(
            ctx,
            r,
            "campaign_makegood_booked",
            makegood=choice,
            makegood_minor=r["free_capacity_minor"],
            makegood_campaign=placement["campaign"],
            free_capacity_minor=0,
        )
    ctx.require(r.get("capacity_reviewed") and (not r.get("activated")), "CAMPAIGN_ACTIVATION")
    amount = r["accepted_capacity_minor"] + r.get("makegood_minor", 0)
    ctx.require(wallet["usable_minor"] >= amount, "WALLET_AVAILABLE")
    ctx.update(
        "xp_b_business_accounts",
        wallet["id"],
        usable_minor=wallet["usable_minor"] - amount,
        campaign_spent_minor=wallet.get("campaign_spent_minor", 0) + amount,
    )
    return mark(
        ctx,
        r,
        "campaign_activated",
        activated=True,
        delivered_minor=r["accepted_capacity_minor"]
        + (r.get("makegood_minor", 0) if r.get("makegood_campaign") == r["campaign"] else 0),
        spent_minor=amount,
    )


expose(
    "reserve_campaign_launch_capacity",
    "xp_b_campaign_launches",
    "reserve",
    campaign,
    "Reserve provider capacity against real usable credit in wallet_id; allocation event publishes accepted capacity. Reserving does not itself activate advertising.",
)
expose(
    "release_unserved_campaign_capacity",
    "xp_b_campaign_launches",
    "release",
    campaign,
    "Release the portion the provider cannot serve. Original accepted capacity stays reserved, and only released capacity can fund a makegood placement.",
)
expose(
    "book_campaign_makegood",
    "xp_b_campaign_launches",
    "makegood",
    campaign,
    "Book released capacity into one listed placement. Placement campaign and booking fee are public; a different campaign consumes credit without advancing this launch.",
    choice=S,
)
expose(
    "activate_campaign_launch",
    "xp_b_campaign_launches",
    "activate",
    campaign,
    "Consume the actual booked provider credits and activate the selected campaigns. Original wallet cash cannot be spent a second time; wrong-campaign booking remains a real loss.",
)


def fulfillment(ctx, r, action, choice=None):
    order = ctx.record("xp_b_store_orders", r["store_order_id"])
    if action == "reserve":
        ctx.require(not r.get("inventory_requested"), "INVENTORY_ALREADY_REQUESTED")
        update(ctx, r, inventory_requested=True)
        event(
            ctx,
            r,
            "inventory",
            {"inventory_reviewed": True, "available_units": r["_available_units"]},
        )
        return {"inventory": "reservation_pending"}
    if action == "substitute":
        ctx.require(
            r.get("inventory_reviewed")
            and choice in r["substitutes"]
            and (not r.get("substitute")),
            "SUBSTITUTE_REQUEST",
        )
        s = r["substitutes"][choice]
        charge(ctx, r, s["fee_minor"], "order_substitute_fee")
        return mark(
            ctx,
            r,
            "order_substitution_committed",
            substitute=choice,
            substitute_units=r["ordered_units"] - r["available_units"],
            substitute_compatible=s["socket"] == r["required_socket"]
            and s["rating"] >= r["required_rating"],
        )
    if action == "dispatch":
        ctx.require(
            order["status"] == "paid" and r.get("inventory_reviewed") and (not r.get("dispatched")),
            "PAID_FULFILLMENT_REQUIRED",
        )
        units = r["available_units"] + r.get("substitute_units", 0)
        update(
            ctx,
            r,
            dispatched=True,
            dispatch_units=units,
            paid_receipt_ids=deepcopy(order["receipt_payment_ids"]),
        )
        event(ctx, r, "delivery", {"delivered_units": units, "delivery_complete": True})
        return {"dispatched_units": units, "merchant_account": order["merchant_account"]}
    ctx.require(r.get("delivery_complete") and (not r.get("closed")), "DELIVERY_RECEIPT_REQUIRED")
    return mark(
        ctx,
        r,
        "store_fulfillment_closed",
        closed=True,
        merchant_account=order["merchant_account"],
        receipt_net_minor=order["net_received_minor"],
    )


expose(
    "reserve_store_order_inventory",
    "xp_b_order_fulfillments",
    "reserve",
    fulfillment,
    "Reserve stock for the linked existing storefront order. Inventory receipt may reveal a short line independently of payment reconciliation.",
)
expose(
    "commit_store_order_substitute",
    "xp_b_order_fulfillments",
    "substitute",
    fulfillment,
    "Commit a listed substitution for unavailable units. Compatibility and fee are public; an incompatible substitute or omission can leave the commercial order unfulfilled.",
    choice=S,
)
expose(
    "dispatch_reconciled_store_order",
    "xp_b_order_fulfillments",
    "dispatch",
    fulfillment,
    "Dispatch only once the linked merchant order is genuinely paid. Dispatch captures the actual payment component IDs and sends only stocked/substituted units.",
)
expose(
    "close_store_delivery_receipt",
    "xp_b_order_fulfillments",
    "close",
    fulfillment,
    "Close fulfillment against the carrier receipt and the reconciled merchant account. This does not create or replay a customer charge.",
)


def platform(ctx, r, action, choice=None):
    if action == "register":
        ctx.require(not r.get("registered"), "PLATFORM_RECONCILIATION_EXISTS")
        return mark(ctx, r, "platform_reconciliation_registered", registered=True)
    if action == "release":
        ctx.require(
            r.get("hold_posted") and (not r.get("release_requested")), "PLATFORM_HOLD_NOT_POSTED"
        )
        update(ctx, r, release_requested=True)
        event(
            ctx,
            r,
            "reserve_review",
            {"reserve_reviewed": True, "disputed_minor": r["_disputed_minor"]},
        )
        return {"reserve_review": "pending"}
    if action == "dispose":
        ctx.require(
            r.get("reserve_reviewed")
            and choice in ("evidence_appeal", "waive_disputed", "forfeit_all")
            and (not r.get("reserve_disposition")),
            "PLATFORM_RESERVE_DISPOSITION",
        )
        charge(
            ctx,
            r,
            r["appeal_fee_minor"] if choice == "evidence_appeal" and r["disputed_minor"] else 0,
            "platform_hold_appeal_fee",
        )
        released = (
            r["hold_minor"]
            if choice == "evidence_appeal"
            else r["hold_minor"] - r["disputed_minor"] if choice == "waive_disputed" else 0
        )
        post(ctx, r, released, "platform_reserve_release")
        return mark(
            ctx,
            r,
            "platform_reserve_disposed",
            reserve_disposition=choice,
            reserve_released_minor=released,
        )
    receiving = ctx.record("xp_b_receiving_accounts", r["receiving_id"])
    ctx.require(
        receiving["status"] == "active"
        and receiving["manual_supported"]
        and (not r.get("manual_received")),
        "MANUAL_RECEIPT_UNAVAILABLE",
    )
    post(ctx, r, r["manual_receipt_minor"], "manual_platform_receipt")
    return mark(
        ctx,
        r,
        "manual_platform_receipt_settled",
        manual_received=True,
        manual_reference=r["manual_reference"],
    )


expose(
    "register_platform_revenue_reconciliation",
    "xp_b_platform_reconciliations",
    "register",
    platform,
    "Register reconciliation of the selected platform receiving account. Its first accepted-payout settlement posts the published reserve hold from actual proceeds; Other platform rules are separate.",
)
expose(
    "request_platform_reserve_breakdown",
    "xp_b_platform_reconciliations",
    "release",
    platform,
    "Request the platform's fixed reserve breakdown after a real hold has posted. The event identifies disputed and undisputed amounts.",
)
expose(
    "dispose_platform_revenue_reserve",
    "xp_b_platform_reconciliations",
    "dispose",
    platform,
    "Choose evidence_appeal, waive_disputed or forfeit_all. Appeal uses the published existing settlement evidence and fee; waiver/forfeiture legally surrenders retained proceeds.",
    choice=S,
)
expose(
    "settle_manual_platform_receipt",
    "xp_b_platform_reconciliations",
    "manual",
    platform,
    "Settle the genuine pending manual receipt in this record. Requires the receiving account to remain active; does not enable any automatic payout rule.",
)


def export_reporting(ctx, r, action, amount_minor=None, choice=None):
    lot = ctx.record("xp_b_export_lots", r["lot_id"])
    if action == "register":
        ctx.require(
            not r.get("invoice_basis_registered") and choice in r["invoice_entities"],
            "EXPORT_INVOICE_ENTITY",
        )
        return mark(
            ctx,
            r,
            "export_invoice_basis_registered",
            invoice_basis_registered=True,
            invoice_entity=choice,
        )
    if action == "review":
        ctx.require(
            r.get("invoice_basis_registered") and (not r.get("review_requested")),
            "EXPORT_BASIS_REQUIRED",
        )
        update(ctx, r, review_requested=True)
        event(
            ctx,
            r,
            "export_allocation",
            {"allocation_reviewed": True, "reportable_minor": r["_reportable_minor"]},
        )
        return {"allocation": "pending_customs"}
    if action == "basis":
        ctx.require(
            r.get("allocation_reviewed")
            and lot["status"] == "available"
            and (0 <= amount_minor <= lot["amount_minor"]),
            "EXPORT_BASIS_PARAMETERS",
        )
        ctx.update(
            "xp_b_export_lots",
            lot["id"],
            reporting_basis_minor=amount_minor,
            invoice_entity=r.get("invoice_entity"),
        )
        return mark(
            ctx, r, "declaration_basis_elected", elected_minor=amount_minor, basis_fixed=True
        )
    ctx.require(
        lot.get("declaration_status") == "issued"
        and lot.get("declaration_delivered")
        or (lot.get("product") == "ordinary" and lot.get("status") == "settled"),
        "SETTLED_REPORTING_RECORD_REQUIRED",
    )
    return mark(
        ctx,
        r,
        "export_reporting_submitted",
        report_submitted=True,
        reported_minor=(
            lot.get("reporting_basis_minor", lot["amount_minor"])
            if lot.get("product") == "declaration"
            else 0
        ),
        reported_entity=lot.get("invoice_entity"),
        declaration_id=lot.get("declaration_id"),
        report_product=lot.get("product"),
    )


expose(
    "register_export_invoice_allocation",
    "xp_b_export_reporting",
    "register",
    export_reporting,
    "Register the invoice issuer for the unsettled lot. invoice_entities lists transport-supported entities; the exporter and customs allocation still govern reporting rights.",
    choice=S,
)
expose(
    "review_export_customs_allocation",
    "xp_b_export_reporting",
    "review",
    export_reporting,
    "Ask customs to publish the reportable portion of gross settlement; an adjustment portion can settle as cash without becoming reportable export value.",
)
expose(
    "fix_export_declaration_basis",
    "xp_b_export_reporting",
    "basis",
    export_reporting,
    "Before settlement, elect the monetary declaration basis within gross lot value. The bank accepts a self-declared value; the caller is responsible for matching customs evidence and issuer.",
    amount_minor=I,
)
expose(
    "submit_export_reporting_package",
    "xp_b_export_reporting",
    "submit",
    export_reporting,
    "Submit the issued declaration with the basis fixed before settlement, or a settled ordinary-cash record reporting zero declaration rights. Gross cash conversion and reportable invoice value are separate quantities.",
)


def supplier_fx(ctx, r, action, amount_minor=None, choice=None):
    need = ctx.record("xp_b_fx_requirements", r["requirement_id"])
    if action == "advice":
        ctx.require(not r.get("advice_requested"), "CREDIT_ADVICE_REQUESTED")
        update(ctx, r, advice_requested=True)
        event(
            ctx,
            r,
            "supplier_credit",
            {"advice_received": True, "credit_minor": r["_credit_minor"]},
            3,
        )
        return {"advice": "pending_supplier"}
    if action == "credit":
        ctx.require(
            r.get("advice_received")
            and (not r.get("credit_applied"))
            and (
                choice
                in ("invoice_netting", "cash_refund", "contract_credit", "settlement_discount")
            ),
            "SUPPLIER_CREDIT_DISPOSITION",
        )
        used = r["credit_minor"] if choice == "invoice_netting" else 0
        ctx.update("xp_b_fx_requirements", need["id"], need_minor=r["original_need_minor"] - used)
        if choice == "cash_refund":
            source_gross = r["credit_minor"] * need["rate_num"] // need["rate_den"]
            event(
                ctx,
                r,
                "supplier_cash_refund",
                {"refund_received_minor": max(0, source_gross - r["refund_fee_minor"])},
                r["refund_delay"],
            )
        return mark(
            ctx,
            r,
            "supplier_credit_allocated",
            credit_applied=True,
            credit_choice=choice,
            applied_credit_minor=used,
        )
    if action == "sell":
        ctx.require(
            not need["paid"]
            and 0 < amount_minor <= need["acquired_minor"]
            and (choice in r["sale_routes"]),
            "FX_SALE_PARAMETERS",
        )
        fee = r["sale_routes"][choice]
        proceeds = amount_minor * need["rate_num"] // need["rate_den"] - fee
        ctx.require(proceeds >= 0, "FX_SALE_BELOW_FEE")
        post(ctx, r, proceeds, "supplier_fx_liquidation")
        ctx.update(
            "xp_b_fx_requirements", need["id"], acquired_minor=need["acquired_minor"] - amount_minor
        )
        return mark(
            ctx,
            r,
            "supplier_currency_sold",
            sold_minor=r.get("sold_minor", 0) + amount_minor,
            sale_proceeds_minor=r.get("sale_proceeds_minor", 0) + proceeds,
            sale_route=choice,
        )
    ctx.require(need["paid"] and r.get("credit_applied"), "SUPPLIER_PAYMENT_REQUIRED")
    return mark(
        ctx,
        r,
        "supplier_fx_confirmation",
        confirmed=True,
        confirmed_payable_minor=need["need_minor"],
        unspent_foreign_minor=need["remaining_minor"],
    )


expose(
    "request_supplier_credit_advice",
    "xp_b_supplier_fx_credits",
    "advice",
    supplier_fx,
    "Request the supplier's amended-invoice credit advice. The fixed event can arrive while an older FX cancellation is pending and change the underlying currency requirement.",
)
expose(
    "allocate_supplier_invoice_credit",
    "xp_b_supplier_fx_credits",
    "credit",
    supplier_fx,
    "Choose invoice_netting, cash_refund, contract_credit or settlement_discount for the confirmed credit. Netting reduces this invoice's payable; a cash refund leaves the gross payment due and returns credit less refund_fee_minor after refund_delay. Contract credit retains it against the separately listed future contract; a settlement discount surrenders it as a commercial concession.",
    choice=S,
)
expose(
    "sell_surplus_supplier_currency",
    "xp_b_supplier_fx_credits",
    "sell",
    supplier_fx,
    "Sell held target currency before supplier payment using a public sale_routes quote. Reduces acquired currency, credits actual net sale proceeds, and cannot reverse an already executed supplier payment.",
    amount_minor=I,
    choice=S,
)
expose(
    "confirm_supplier_fx_settlement",
    "xp_b_supplier_fx_credits",
    "confirm",
    supplier_fx,
    "Confirm the paid revised invoice and actual unspent currency. Does not conceal surplus or repair an incorrect credit allocation.",
)


def royalty(ctx, r, action, choice=None, amount_minor=None):
    pool = ctx.record("xp_b_pools", r["pool_id"])
    if action == "review":
        ctx.require(not r.get("review_requested"), "RESIDENCY_REVIEW_EXISTS")
        update(ctx, r, review_requested=True)
        event(
            ctx,
            r,
            "royalty_residency",
            {"residency_reviewed": True, "withholding_bps": r["_withholding_bps"]},
        )
        return {"residency_review": "pending"}
    if action == "elect":
        ctx.require(
            r.get("residency_reviewed")
            and any(
                (
                    a["id"] == choice and a["pool_id"] == pool["id"]
                    for a in ctx.records("xp_b_allocations")
                )
            ),
            "WITHHOLDING_ELECTION",
        )
        return mark(
            ctx,
            r,
            "royalty_withholding_elected",
            withholding_choice=choice,
            elected_tax_minor=amount_minor,
        )
    if action == "remit":
        ctx.require(
            pool["status"] == "converted"
            and r.get("withholding_choice")
            and (not r.get("tax_remitted")),
            "ROYALTY_REMITTANCE",
        )
        amount = r["elected_tax_minor"]
        ctx.require(amount <= pool["remaining_minor"], "ROYALTY_POOL_BALANCE")
        beneficiary = r["withholding_choice"]
        ctx.update("xp_b_pools", pool["id"], remaining_minor=pool["remaining_minor"] - amount)
        return mark(
            ctx,
            r,
            "royalty_tax_remitted",
            tax_remitted=True,
            withheld_minor=amount,
            withheld_beneficiary_id=beneficiary,
        )
    ctx.require(
        pool["status"] == "converted" and r.get("residency_reviewed"), "ROYALTY_CERTIFICATE"
    )
    return mark(
        ctx,
        r,
        "royalty_certificate_delivered",
        certificate_delivered=True,
        certificate_tax_minor=r.get("withheld_minor", 0),
        certificate_beneficiary_id=r.get("withheld_beneficiary_id"),
    )


expose(
    "review_royalty_beneficiary_residency",
    "xp_b_royalty_tax",
    "review",
    royalty,
    "Publish residency-dependent withholding for the named beneficiary. Contractual gross shares still follow the original largest-remainder rule.",
)
expose(
    "elect_royalty_withholding",
    "xp_b_royalty_tax",
    "elect",
    royalty,
    "Elect tax amount and attribution using choice as an actual allocation ID from the same pool. The bank can accept declarations, but withheld tax belongs to the actual liable beneficiary and reduces that beneficiary's net cash.",
    choice=S,
    amount_minor=I,
)
expose(
    "remit_royalty_withholding",
    "xp_b_royalty_tax",
    "remit",
    royalty,
    "Remit elected withholding from the same converted pool used for net beneficiary distributions. The same minor unit cannot be paid to a beneficiary and tax authority.",
)
expose(
    "deliver_royalty_tax_certificate",
    "xp_b_royalty_tax",
    "certificate",
    royalty,
    "Deliver the actual beneficiary/tax attribution after distribution. A certificate cannot retroactively repair money attributed to another beneficiary.",
)


def window(ctx, r, action, choice=None):
    if action == "book":
        ctx.require(not r.get("window_booked") and choice in r["windows"], "SETTLEMENT_WINDOW")
        spec = r["windows"][choice]
        charge(ctx, r, spec["fee_minor"], "currency_window_fee")
        update(ctx, r, window_booked=True, window=choice, expires_minute=ctx.minute + spec["ttl"])
        event(
            ctx,
            r,
            "currency_capacity",
            {
                "capacity_confirmed": True,
                "short_pair_id": r["short_pair_id"],
                "confirmed_shortfall_minor": r["_shortfall_minor"],
            },
        )
        return {"capacity_confirmation": "pending", "expires_minute": ctx.minute + spec["ttl"]}
    if action == "assign":
        pair = ctx.record("xp_b_settlement_pairs", choice)
        ctx.require(
            r.get("capacity_confirmed") and ctx.minute <= r["expires_minute"],
            "WINDOW_NOT_EFFECTIVE",
        )
        return mark(
            ctx,
            r,
            "currency_window_pair_assigned",
            assigned_pair_id=pair["id"],
            assigned_target_minor=pair["target_minor"],
        )
    pair = ctx.record("xp_b_settlement_pairs", r.get("assigned_pair_id", ""))
    ctx.require(pair["status"] == "settled", "SETTLED_PAIR_REQUIRED")
    return mark(
        ctx,
        r,
        "currency_finality_certificate",
        finality_confirmed=True,
        settled_pair_id=pair["id"],
        settled_target_minor=pair["received_minor"],
    )


expose(
    "book_currency_settlement_window",
    "xp_b_currency_windows",
    "book",
    window,
    "Book a published settlement window and its fee. A counterparty capacity event may withdraw readiness of a shortlisted pair before principal is committed.",
    choice=S,
)
expose(
    "assign_currency_window_pair",
    "xp_b_currency_windows",
    "assign",
    window,
    "Assign one observed pair to the effective window. Pair selection controls which original prepare/settle operations can use the window; published principal and target amounts remain authoritative.",
    choice=S,
)
expose(
    "confirm_currency_settlement_finality",
    "xp_b_currency_windows",
    "confirm",
    window,
    "Confirm actual received target currency and the committed pair after settlement. Does not reverse unsecured release or expired execution.",
)


def rebate(ctx, r, action, choice=None):
    if action == "trace":
        ctx.require(not r.get("trace_requested"), "REBATE_TRACE_EXISTS")
        charge(ctx, r, r["trace_fee_minor"], "rebate_trace_fee")
        update(ctx, r, trace_requested=True)
        event(
            ctx,
            r,
            "prior_rebate",
            {
                "rebate_located": True,
                "rebate_minor": r["_rebate_minor"],
                "rebate_fee_id": r["_rebate_fee_id"],
            },
        )
        return {"rebate_trace": "pending"}
    if action == "allocate":
        ctx.require(
            r.get("rebate_located") and (not r.get("rebate_allocated")), "REBATE_ALLOCATION"
        )
        bill = ctx.record("xp_b_billing", choice)
        ctx.require(
            bill["provider"] == "bank" and (not bill.get("reviewed")), "REBATE_BILLING_STATE"
        )
        ctx.update(
            "xp_b_billing", bill["id"], refund_minor=bill["refund_minor"] + r["rebate_minor"]
        )
        return mark(
            ctx, r, "prior_rebate_allocated", rebate_allocated=True, allocated_fee_id=bill["id"]
        )
    if action == "statement":
        bills = ctx.records("xp_b_billing")
        ctx.require(all((x.get("basis_status") == "final" for x in bills)), "FINAL_BILLING_BASIS")
        lines = {
            x["id"]: x["refund_minor"]
            for x in bills
            if x["provider"] == "bank" and (not x["missed_coupon"])
        }
        return mark(
            ctx,
            r,
            "fee_recovery_statement_issued",
            statement_lines=lines,
            statement_total_minor=sum(lines.values()),
            statement_issued=True,
        )
    ctx.require(
        r.get("statement_issued") and choice in r["company_destinations"], "REBATE_DELIVERY_TARGET"
    )
    return mark(
        ctx,
        r,
        "fee_recovery_statement_delivered",
        delivered=True,
        delivered_company=r["company_destinations"][choice],
        delivery_address_id=choice,
    )


expose(
    "trace_previously_posted_fee_rebate",
    "xp_b_rebate_reconciliations",
    "trace",
    rebate,
    "Trace an existing bank rebate in the original journal, charging trace_fee_minor. The result publishes amount and original billing reference; it is not a new cash credit.",
)
expose(
    "allocate_existing_fee_rebate",
    "xp_b_rebate_reconciliations",
    "allocate",
    rebate,
    "Allocate located rebate occupancy to a bank billing record. choice is a billing ID. Subsequent fee corrections deduct existing refund_minor, so reference attribution changes the amount still claimable.",
    choice=S,
)
expose(
    "issue_fee_recovery_statement",
    "xp_b_rebate_reconciliations",
    "statement",
    rebate,
    "Issue a company statement of actual bank fee credits after all pending bases finalize. It includes original rebates and newly posted corrections without double-counting.",
)
expose(
    "deliver_fee_recovery_statement",
    "xp_b_rebate_reconciliations",
    "deliver",
    rebate,
    "Deliver the credit statement to a company_destinations address. Listed contacts can belong to separate entities; delivery is irreversible.",
    choice=S,
)


def tax_credit(ctx, r, action, choice=None, amount_minor=None):
    if action == "review":
        ctx.require(not r.get("credit_review_requested"), "TAX_CREDIT_REVIEW_EXISTS")
        update(ctx, r, credit_review_requested=True)
        event(
            ctx,
            r,
            "jurisdiction_credit",
            {"credit_confirmed": True, "credit_minor": r["_credit_minor"]},
        )
        return {"tax_credit": "pending_authority"}
    if action == "allocate":
        ctx.require(
            r.get("credit_confirmed")
            and (not r.get("credit_allocated"))
            and (amount_minor <= r["credit_minor"]),
            "TAX_CREDIT_AVAILABLE",
        )
        obligation = ctx.record("xp_b_tax_obligations", choice)
        ctx.require(
            not obligation["paid"] and amount_minor <= obligation["amount_minor"],
            "TAX_OBLIGATION_ALLOCATION",
        )
        ctx.update(
            "xp_b_tax_obligations",
            obligation["id"],
            amount_minor=obligation["amount_minor"] - amount_minor,
            credit_minor=amount_minor,
            credit_reference=r["credit_reference"],
        )
        return mark(
            ctx,
            r,
            "jurisdiction_credit_allocated",
            credit_allocated=True,
            allocated_obligation_id=obligation["id"],
            allocated_minor=amount_minor,
        )
    obligation = ctx.record("xp_b_tax_obligations", choice)
    ctx.require(obligation["paid"] and obligation["discharged"], "TAX_RECEIPT_NOT_AVAILABLE")
    receipts = dict(r.get("receipts", {}))
    ctx.require(obligation["id"] not in receipts, "TAX_RECEIPT_ALREADY_DELIVERED")
    receipts[obligation["id"]] = {
        "cash_minor": obligation["amount_minor"],
        "credit_minor": obligation.get("credit_minor", 0),
        "jurisdiction": obligation["jurisdiction"],
        "recipient_id": obligation["recipient_id"],
        "paid_minute": obligation["paid_minute"],
    }
    return mark(ctx, r, "jurisdiction_tax_receipt_delivered", receipts=receipts)


expose(
    "review_jurisdiction_tax_credit",
    "xp_b_jurisdiction_credits",
    "review",
    tax_credit,
    "Ask the named authority to confirm credit from the original tax reference. The result may reduce only the matching jurisdiction's commercial liability.",
)
expose(
    "allocate_jurisdiction_tax_credit",
    "xp_b_jurisdiction_credits",
    "allocate",
    tax_credit,
    "Allocate confirmed credit to an unpaid obligation ID with amount_minor. Bank allocation is a bookkeeping instruction; matching jurisdiction and reference remain the caller's responsibility. Changes the original cash payment amount.",
    choice=S,
    amount_minor=I,
)
expose(
    "deliver_jurisdiction_tax_receipt",
    "xp_b_jurisdiction_credits",
    "receipt",
    tax_credit,
    "Deliver a receipt for one actually paid tax obligation ID. Separates cash, attributed credit, recipient and jurisdiction so a cross-jurisdiction accounting entry cannot masquerade as legal discharge.",
    choice=S,
)


def tax_appeal(ctx, r, action, choice=None):
    claim = ctx.record("xp_b_tax_claims", r["claim_id"])
    period = ctx.record("xp_b_next_tax", r["period_id"])
    if action == "appeal":
        ctx.require(
            claim["status"] == "approved"
            and (not r.get("appeal_requested"))
            and (choice in r["appeal_channels"]),
            "TAX_APPEAL_STATE",
        )
        channel = r["appeal_channels"][choice]
        charge(ctx, r, channel["fee_minor"], "tax_appeal_fee")
        update(ctx, r, appeal_requested=True, appeal_channel=choice)
        event(
            ctx,
            r,
            "tax_appeal",
            {"appeal_complete": True, "additional_credit_minor": r["_additional_credit_minor"]},
            channel["delay"],
        )
        return {"appeal": "pending", "due_minute": ctx.minute + channel["delay"]}
    if action == "notice":
        ctx.require(
            period["paid_minor"] + period["offset_minor"] == period["amount_minor"]
            and (not r.get("notice_requested")),
            "PAID_PERIOD_NOTICE",
        )
        update(ctx, r, notice_requested=True)
        event(
            ctx,
            r,
            "tax_notice",
            {
                "notice_received": True,
                "notice_cash_minor": period["paid_minor"],
                "notice_offset_minor": period["offset_minor"],
                "notice_period_id": period["id"],
            },
        )
        return {"authority_notice": "pending"}
    ctx.require(
        r.get("notice_received") and choice in r["notice_references"], "TAX_CLEARANCE_REFERENCE"
    )
    return mark(
        ctx,
        r,
        "tax_clearance_acknowledged",
        clearance_acknowledged=True,
        clearance_reference=choice,
        cleared_period_id=r["notice_references"][choice],
    )


expose(
    "appeal_unapproved_tax_balance",
    "xp_b_tax_appeals",
    "appeal",
    tax_appeal,
    "Appeal only after the original authority review. appeal_channels publishes cost/delay; confirmed additional credit becomes part of the same claim and can fund the next period before its deadline.",
    choice=S,
)
expose(
    "request_next_tax_clearance_notice",
    "xp_b_tax_appeals",
    "notice",
    tax_appeal,
    "Request authority clearance after actual cash plus offsets discharge the next period. The event publishes the credited components and period identity.",
)
expose(
    "acknowledge_tax_clearance_reference",
    "xp_b_tax_appeals",
    "acknowledge",
    tax_appeal,
    "Acknowledge an authority notice reference from notice_references. References may concern different periods; the customer needs clearance of this paid period.",
    choice=S,
)


def customs_lien(ctx, r, action, choice=None):
    importer = ctx.record("xp_b_importers", r["importer_id"])
    if action == "inspect":
        ctx.require(not r.get("lien_review_requested"), "LIEN_REVIEW_EXISTS")
        update(ctx, r, lien_review_requested=True)
        event(
            ctx,
            r,
            "customs_lien",
            {
                "lien_reviewed": True,
                "lien_minor": r["_lien_minor"],
                "lien_entry_id": r["_lien_entry_id"],
            },
        )
        return {"lien_review": "pending_registry"}
    if action == "file":
        ctx.require(
            r.get("lien_reviewed")
            and (not r.get("release_filed"))
            and (choice in r["release_certificates"]),
            "LIEN_RELEASE_CERTIFICATE",
        )
        cert = r["release_certificates"][choice]
        charge(ctx, r, cert["fee_minor"], "customs_lien_release_fee")
        update(ctx, r, release_filed=True, certificate_id=choice)
        event(
            ctx,
            r,
            "lien_discharge",
            {
                "discharge_received": True,
                "discharged_entry_id": cert["entry_id"],
                "discharged_minor": cert["amount_minor"],
            },
            cert["delay"],
        )
        return {"lien_discharge": "pending_registry"}
    if action == "apply":
        ctx.require(
            r.get("discharge_received") and (not r.get("lien_released")), "LIEN_DISCHARGE_RECEIPT"
        )
        amount = (
            min(r["lien_minor"], r["discharged_minor"])
            if r["discharged_entry_id"] == r["lien_entry_id"]
            else 0
        )
        if importer.get("award_status") == "received":
            dest = ctx.record("xp_b_destinations", importer["destination_id"])
            enc = max(0, r["lien_minor"] - amount)
            ctx.update(
                "xp_b_destinations",
                dest["id"],
                encumbered_minor=enc,
                available_minor=dest["received_minor"] - enc,
            )
        return mark(
            ctx, r, "customs_encumbrance_released", lien_released=True, release_minor=amount
        )
    ctx.require(importer.get("award_status") == "received", "CUSTOMS_AWARD_RECEIPT")
    return mark(
        ctx,
        r,
        "customs_funds_availability_statement",
        statement_delivered=True,
        award_minor=importer["award_minor"],
        encumbered_minor=max(0, r.get("lien_minor", 0) - r.get("release_minor", 0)),
        account_id_at_delivery=importer["destination_id"],
    )


expose(
    "inspect_importer_customs_lien",
    "xp_b_customs_liens",
    "inspect",
    customs_lien,
    "Inspect registry encumbrances on the importer's duty award. An eligible refund can remain legally encumbered even when credited to the correct ACH account.",
)
expose(
    "file_customs_lien_discharge",
    "xp_b_customs_liens",
    "file",
    customs_lien,
    "File one registry-issued release certificate. release_certificates publishes covered entry, amount and fee; similarly named certificates can cover different duty entries.",
    choice=S,
)
expose(
    "apply_customs_encumbrance_release",
    "xp_b_customs_liens",
    "apply",
    customs_lien,
    "Apply the registry's actual discharge to the matching entry lien. An unrelated certificate is legally filed but releases no encumbrance on this award.",
)
expose(
    "deliver_customs_availability_statement",
    "xp_b_customs_liens",
    "statement",
    customs_lien,
    "Deliver the award's received amount and outstanding legal encumbrance after ACH receipt. The statement cannot erase an unreleased lien.",
)


def invoice_identity(ctx, r, action, choice=None):
    profile = ctx.record("xp_b_invoice_profiles", r["profile_id"])
    if action == "review":
        ctx.require(not r.get("requirements_requested"), "BUYER_REQUIREMENTS_EXISTS")
        update(ctx, r, requirements_requested=True)
        event(
            ctx,
            r,
            "buyer_billing",
            {
                "requirements_received": True,
                "required_address": r["_required_address"],
                "buyer_reference": r["_buyer_reference"],
            },
        )
        return {"billing_requirements": "pending_buyer"}
    if action == "identity":
        ctx.require(
            r.get("requirements_received") and choice in r["registered_addresses"],
            "REGISTERED_COMPANY_ADDRESS",
        )
        address = r["registered_addresses"][choice]
        ctx.update(
            "xp_b_invoice_profiles",
            profile["id"],
            address=address,
            address_version=profile.get("address_version", 1) + 1,
        )
        return mark(
            ctx,
            r,
            "invoice_identity_updated",
            selected_address_id=choice,
            address_version=profile["address_version"],
        )
    if action == "route":
        ctx.require(
            choice in r["delivery_routes"] and (not r.get("delivery_route")),
            "INVOICE_DELIVERY_ROUTE",
        )
        return mark(
            ctx,
            r,
            "invoice_delivery_route_bound",
            delivery_route=choice,
            recipient_company=r["delivery_routes"][choice],
        )
    invoices = [
        x
        for x in ctx.records("xp_b_invoices")
        if x["status"] == "delivered" and x["company"] == profile["company"]
    ]
    ctx.require(
        invoices and r.get("delivery_route") and r.get("requirements_received"),
        "INVOICE_DELIVERY_REQUIRED",
    )
    accepted = (
        all((x["address"] == r["required_address"] for x in invoices))
        and r["recipient_company"] == profile["company"]
    )
    return mark(
        ctx,
        r,
        "buyer_invoice_receipt",
        buyer_accepted=accepted,
        buyer_invoice_ids=[x["id"] for x in invoices],
        delivered_addresses=[x["address"] for x in invoices],
    )


expose(
    "request_buyer_invoice_requirements",
    "xp_b_invoice_identities",
    "review",
    invoice_identity,
    "Retrieve the buyer's signed billing-address requirement before issuing replacement service-fee invoices. The requirement and current company master data can differ.",
)
expose(
    "update_company_invoice_address",
    "xp_b_invoice_identities",
    "identity",
    invoice_identity,
    "Select a registered company address ID and increment the original invoice profile's address version. Already issued invoices retain the old address; changing master data does not rewrite them.",
    choice=S,
)
expose(
    "bind_company_invoice_delivery",
    "xp_b_invoice_identities",
    "route",
    invoice_identity,
    "Bind invoice delivery to a listed delivery route. Separate company contacts can share a directory; the route determines the receiving entity.",
    choice=S,
)
expose(
    "obtain_buyer_invoice_acknowledgement",
    "xp_b_invoice_identities",
    "acknowledge",
    invoice_identity,
    "Obtain the buyer's acceptance or rejection based on the actual delivered invoice address and receiving entity. Existing fee occupancy and replacement costs remain in force.",
)


def evidence_access(ctx, r, action, choice=None):
    evidence_request = ctx.record("xp_b_evidence_requests", r["evidence_request_id"])
    if action == "verify":
        ctx.require(
            not r.get("recipient_verification_requested") and choice in r["verification_channels"],
            "RECIPIENT_VERIFICATION",
        )
        channel = r["verification_channels"][choice]
        charge(ctx, r, channel["fee_minor"], "proof_recipient_verification")
        update(ctx, r, recipient_verification_requested=True)
        event(
            ctx,
            r,
            "proof_recipient",
            {
                "recipient_verified": True,
                "required_fields": r["_required_fields"],
                "verified_contact": r["verified_contact"],
            },
            channel["delay"],
        )
        return {"recipient_verification": "pending"}
    if action == "policy":
        ctx.require(
            r.get("recipient_verified") and choice in r["access_policies"], "PROOF_ACCESS_POLICY"
        )
        return mark(
            ctx,
            r,
            "proof_access_policy_bound",
            access_policy=choice,
            authorized_readers=r["access_policies"][choice],
        )
    if action == "link":
        evidence = ctx.record("xp_b_bank_evidence", choice)
        ctx.require(
            evidence["delivered"] and r.get("access_policy") and (not r.get("link_issued")),
            "PROOF_LINK_PREREQUISITES",
        )
        update(
            ctx,
            r,
            link_issued=True,
            linked_evidence_id=evidence["id"],
            link_active=True,
            linked_field_names=deepcopy(evidence["field_names"]),
            link_readers=deepcopy(r["authorized_readers"]),
        )
        event(ctx, r, "proof_delivery", {"recipient_read": True, "reader": r["verified_contact"]})
        return {"link_active": True, "authorized_readers": r["authorized_readers"]}
    ctx.require(r.get("recipient_read") and r.get("link_active"), "PROOF_RECEIPT_REQUIRED")
    return mark(
        ctx, r, "proof_access_link_revoked", link_active=False, revoked_after_reader=r["reader"]
    )


expose(
    "verify_bank_proof_recipient",
    "xp_b_proof_access",
    "verify",
    evidence_access,
    "Verify the recipient through one listed channel, paying its fee. The signed receipt may add fields that the buyer needs; it does not expand customer disclosure consent.",
    choice=S,
)
expose(
    "set_bank_proof_access_scope",
    "xp_b_proof_access",
    "policy",
    evidence_access,
    "Choose a reader policy from access_policies. The transport can support public/team/named readers; only customer-approved recipients may receive this payment proof.",
    choice=S,
)
expose(
    "issue_scoped_bank_proof_link",
    "xp_b_proof_access",
    "link",
    evidence_access,
    "Issue an access link to an actually delivered bank evidence ID. Its immutable fields and reader policy apply until revoked; the recipient read arrives as an event.",
    choice=S,
)
expose(
    "revoke_delivered_bank_proof_link",
    "xp_b_proof_access",
    "revoke",
    evidence_access,
    "Revoke the temporary link after the genuine recipient receipt. This closes future access without pretending to undo information already disclosed.",
)


def beneficiary(ctx, r, action, choice=None):
    obligation = ctx.record("xp_b_secure_obligations", r["obligation_id"])
    if action == "verify":
        ctx.require(
            not r.get("verification_requested") and choice in r["verification_channels"],
            "BENEFICIARY_VERIFICATION",
        )
        spec = r["verification_channels"][choice]
        charge(ctx, r, spec["fee_minor"], "supplier_beneficiary_verification")
        update(ctx, r, verification_requested=True, verification_channel=choice)
        event(
            ctx,
            r,
            "supplier_beneficiary",
            {
                "verification_complete": True,
                "confirmed_beneficiary_id": r["_confirmed_beneficiary_id"],
            },
            spec["delay"],
        )
        return {"beneficiary_confirmation": "pending_registered_contact"}
    if action == "authorize":
        ctx.require(
            choice in r["beneficiaries"]
            and (r.get("verification_complete") or not r["amendment_pending"]),
            "BENEFICIARY_AUTHORIZATION",
        )
        return mark(
            ctx,
            r,
            "supplier_beneficiary_authorized",
            authorized_beneficiary_id=choice,
            authorized_amount_minor=obligation["amount_minor"],
            authorization_minute=ctx.minute,
        )
    ctx.require(
        obligation.get("paid") and (not r.get("receipt_requested")), "SUPPLIER_PAYMENT_RECEIPT"
    )
    update(ctx, r, receipt_requested=True)
    event(
        ctx,
        r,
        "supplier_credit_receipt",
        {
            "supplier_receipt_received": True,
            "credited_beneficiary_id": obligation.get("beneficiary_id"),
            "credited_minor": obligation["amount_minor"],
        },
    )
    return {"supplier_credit_receipt": "pending_bank"}


expose(
    "verify_supplier_beneficiary_amendment",
    "xp_b_supplier_beneficiaries",
    "verify",
    beneficiary,
    "Verify changed supplier payment details through a registered contact channel. The fixed result publishes the confirmed account; an amendment request alone is not proof of ownership.",
    choice=S,
)
expose(
    "authorize_supplier_beneficiary",
    "xp_b_supplier_beneficiaries",
    "authorize",
    beneficiary,
    "Authorize a directory beneficiary ID and the current invoice amount. This sets payment terms but the bank can route to any listed account; verified ownership and the customer's supplier goal must still match.",
    choice=S,
)
expose(
    "request_supplier_credit_receipt",
    "xp_b_supplier_beneficiaries",
    "receipt",
    beneficiary,
    "Request actual receiving-account confirmation after authenticated payment. It publishes the account and amount actually credited, and cannot reverse a wrong beneficiary transfer.",
)


def credential_continuity(ctx, r, action, choice=None):
    if action == "freeze":
        ctx.require(r["standing_status"] == "active", "STANDING_INSTRUCTION_NOT_ACTIVE")
        return mark(ctx, r, "standing_instruction_frozen", standing_status="frozen")
    if action == "audit":
        ctx.require(not r.get("callback_audit_requested"), "CALLBACK_AUDIT_EXISTS")
        update(ctx, r, callback_audit_requested=True)
        event(
            ctx,
            r,
            "callback_audit",
            {"callback_audited": True, "delivery_endpoint_id": r["_delivery_endpoint_id"]},
        )
        return {"callback_delivery_audit": "pending"}
    if action == "rotate":
        ctx.require(
            r.get("callback_audited") and choice in r["callback_endpoints"], "CALLBACK_ENDPOINT"
        )
        return mark(
            ctx,
            r,
            "payment_callback_rotated",
            callback_endpoint_id=choice,
            callback_version=r["callback_version"] + 1,
            old_callback_revoked=True,
        )
    if action == "cancel":
        ctx.require(r["standing_status"] in ("active", "frozen"), "STANDING_CANCELLATION_STATE")
        return mark(ctx, r, "standing_instruction_cancelled", standing_status="cancelled")
    payment = ctx.record("xp_b_security_transfers", r["legitimate_transfer_id"])
    ctx.require(
        payment["status"] == "executed" and r.get("old_callback_revoked"),
        "SUPPLIER_CALLBACK_PENDING",
    )
    return mark(
        ctx,
        r,
        "supplier_payment_callback_delivered",
        supplier_callback_delivered=True,
        callback_recipient=r["callback_endpoints"][r["callback_endpoint_id"]],
        callback_payment_id=payment["id"],
    )


expose(
    "freeze_independent_standing_payment",
    "xp_b_credential_continuity",
    "freeze",
    credential_continuity,
    "Freeze the separately authorized recurring payment before its next due event. Revoking an API credential does not revoke this bank-side standing authority.",
)
expose(
    "audit_payment_callback_destination",
    "xp_b_credential_continuity",
    "audit",
    credential_continuity,
    "Inspect the actual callback endpoint used by the compromised integration; its result distinguishes similarly named supplier and integration endpoints.",
)
expose(
    "rotate_supplier_payment_callback",
    "xp_b_credential_continuity",
    "rotate",
    credential_continuity,
    "Rotate the callback credential and bind one callback_endpoints ID. Existing API-family revocation does not change this independent credential, and choosing another entity can leak payment confirmation.",
    choice=S,
)
expose(
    "cancel_independent_standing_payment",
    "xp_b_credential_continuity",
    "cancel",
    credential_continuity,
    "Cancel the active/frozen standing authority permanently. Frozen authority remains potentially resumable until cancelled; previously accepted transfers remain separately actionable.",
)
expose(
    "deliver_supplier_payment_callback",
    "xp_b_credential_continuity",
    "deliver",
    credential_continuity,
    "Deliver the actual legitimate payment confirmation through the rotated callback endpoint. The destination entity is fixed by the endpoint, not by a familiar display name.",
)


def countersign(ctx, r, action, choice=None, amount_minor=None):
    if action == "request":
        ctx.require(
            not r.get("countersign_requested") and choice in r["officer_channels"],
            "OFFICER_CHANNEL",
        )
        update(ctx, r, countersign_requested=True, officer_channel=choice)
        event(
            ctx,
            r,
            "officer_countersign",
            {
                "officer_acknowledged": True,
                "acknowledged_business_id": r["officer_channels"][choice],
                "acknowledged_limit_minor": r["_acknowledged_limit_minor"],
            },
        )
        return {"officer_countersign": "pending"}
    if action == "limit":
        ctx.require(
            r.get("officer_acknowledged") and amount_minor <= r["acknowledged_limit_minor"],
            "OFFICER_LIMIT",
        )
        return mark(
            ctx,
            r,
            "temporary_delegate_payment_limit",
            payment_limit_minor=amount_minor,
            limit_business_id=r["acknowledged_business_id"],
        )
    if action == "session":
        ctx.require(
            choice in r["report_audiences"] and (not r.get("report_audience")),
            "REPORT_SESSION_AUDIENCE",
        )
        return mark(
            ctx,
            r,
            "delegate_report_session_opened",
            report_audience=choice,
            session_business_ids=r["report_audiences"][choice],
            session_active=True,
        )
    grant = ctx.record("xp_b_delegate_grants", r["grant_id"])
    ctx.require(
        r.get("payment_limit_used") and r.get("session_active"), "TEMPORARY_GRANT_WORK_PENDING"
    )
    kept = [x for x in grant["permissions"] if x not in r["temporary_permissions"]]
    ctx.update("xp_b_delegate_grants", grant["id"], permissions=kept)
    return mark(
        ctx,
        r,
        "temporary_delegate_authority_expired",
        session_active=False,
        temporary_authority_expired=True,
    )


expose(
    "request_company_officer_countersign",
    "xp_b_delegate_countersigns",
    "request",
    countersign,
    "Request a company officer acknowledgement through an officer_channels entry. Delegation review and an officer's payment limit are distinct company-specific authorizations.",
    choice=S,
)
expose(
    "set_temporary_delegate_payment_limit",
    "xp_b_delegate_countersigns",
    "limit",
    countersign,
    "Set an amount limit under the actually acknowledged business identity. The original delegated payment consumes this limit; query/submit permissions alone cannot fund it.",
    amount_minor=I,
)
expose(
    "open_delegate_report_session",
    "xp_b_delegate_countersigns",
    "session",
    countersign,
    "Open a temporary report session for one report_audiences scope. The existing field grants still apply independently to each delivered business.",
    choice=S,
)
expose(
    "expire_temporary_delegate_authority",
    "xp_b_delegate_countersigns",
    "expire",
    countersign,
    "Expire the added temporary permissions and report session after the authorized payment consumes its limit. Existing baseline permissions remain available.",
)
FAMILIES = {
    -34: "xp_b_delivery_contracts",
    -35: "xp_b_campaign_launches",
    34: "xp_b_order_fulfillments",
    35: "xp_b_platform_reconciliations",
    -36: "xp_b_export_reporting",
    36: "xp_b_supplier_fx_credits",
    37: "xp_b_royalty_tax",
    -37: "xp_b_currency_windows",
    -38: "xp_b_rebate_reconciliations",
    38: "xp_b_jurisdiction_credits",
    -39: "xp_b_tax_appeals",
    39: "xp_b_customs_liens",
    -40: "xp_b_invoice_identities",
    40: "xp_b_proof_access",
    41: "xp_b_supplier_beneficiaries",
    42: "xp_b_credential_continuity",
    -41: "xp_b_delegate_countersigns",
}


def register(operations, old_event):
    operations.update(OPERATIONS)

    def records(ctx, n):
        return ctx.records(FAMILIES[n])

    def wrap(name, before=None, after=None):
        original = operations[name]["function"]

        def execute(ctx, **args):
            if before:
                before(ctx, args)
            result = original(ctx, **args)
            return after(ctx, args, result) if after else result

        operations[name]["function"] = execute

    def purchase_before(ctx, args):
        for r in records(ctx, -34):
            if r["order_id"] == args["order_id"]:
                ctx.require(r.get("terms_fixed"), "DELIVERY_RISK_TERMS_REQUIRED")

    def purchase_after(ctx, args, result):
        for r in records(ctx, -34):
            if r["order_id"] == args["order_id"]:
                ctx.update("xp_b_orders", r["order_id"], delivery_risk_term=r["risk_term"])
        return result

    wrap("pay_purchase_order", purchase_before, purchase_after)
    operations["pay_purchase_order"][
        "description"
    ] += " A linked delivery contract must have its risk terms fixed before payment; those terms are recorded on the paid order."

    def payout_before(ctx, args):
        for r in records(ctx, 35):
            ctx.require(r.get("registered"), "PLATFORM_RECONCILIATION_REQUIRED")

    def payout_after(ctx, args, result):
        for r in records(ctx, 35):
            if (
                args["disposition"] == "settle"
                and result["receiving_id"] == r["receiving_id"]
                and (not r.get("hold_posted"))
            ):
                charge(ctx, r, r["hold_minor"], "platform_proceeds_hold")
                update(ctx, r, hold_posted=True, hold_payout_id=result["id"])
        return result

    wrap("resolve_accepted_payout", payout_before, payout_after)
    operations["resolve_accepted_payout"][
        "description"
    ] += (
        " A linked reconciliation posts its reserve hold from the first matching actual settlement."
    )

    def export_before(ctx, args):
        for r in records(ctx, -36):
            if r["lot_id"] == args["lot_id"]:
                ctx.require(r.get("basis_fixed"), "EXPORT_REPORTING_BASIS_REQUIRED")

    wrap("settle_export_proceeds", export_before)
    operations["settle_export_proceeds"][
        "description"
    ] += " For a linked customs allocation, fix the declaration basis and issuer before settling the reportable lot."

    def currency_before(ctx, args):
        for r in records(ctx, -37):
            ctx.require(
                r.get("assigned_pair_id") == args["pair_id"]
                and ctx.minute <= r.get("expires_minute", -1),
                "SETTLEMENT_WINDOW_OR_ASSIGNMENT",
            )

    wrap("settle_currency_pair", currency_before)
    operations["settle_currency_pair"][
        "description"
    ] += " A linked currency window must be unexpired and assigned to this exact pair."
    original_fee = operations["request_fee_correction"]["function"]

    def fee(ctx, fee_id, amount_minor):
        row = ctx.record("xp_b_billing", fee_id)
        prior = row.get("refund_minor", 0)
        proper = (
            row["base_minor"]
            * (
                row["low_rate_bps"]
                if row["eligible_volume_minor"] >= row["tier_threshold_minor"]
                else row["high_rate_bps"]
            )
            // 10000
        )
        remaining = max(0, row["charged_minor"] - proper - prior)
        result = original_fee(ctx, fee_id, amount_minor if amount_minor <= remaining else 0)
        ctx.update("xp_b_billing", fee_id, refund_minor=prior + result["refund_minor"])
        return result

    operations["request_fee_correction"]["function"] = fee
    operations["request_fee_correction"][
        "description"
    ] += " Existing refund_minor occupies the entitlement: a claim above gross overcharge minus previously allocated credits is rejected after its review fee."

    def award_after(ctx, args, result):
        for r in records(ctx, 39):
            importer = ctx.record("xp_b_importers", r["importer_id"])
            if importer.get("award_status") == "received":
                dest = ctx.record("xp_b_destinations", importer["destination_id"])
                enc = max(0, r.get("lien_minor", 0) - r.get("release_minor", 0))
                ctx.update(
                    "xp_b_destinations",
                    dest["id"],
                    encumbered_minor=enc,
                    available_minor=dest["received_minor"] - enc,
                )
        return result

    wrap("accept_customs_award", after=award_after)
    operations["accept_customs_award"][
        "description"
    ] += " A registry lien can make part of credited funds unavailable; a receipt does not extinguish that encumbrance."
    original_challenge = operations["create_payment_challenge"]["function"]
    original_payment = operations["execute_authenticated_payment"]["function"]

    def challenge(ctx, beneficiary_id=None, **args):
        for r in records(ctx, 41):
            if r["obligation_id"] == args["obligation_id"]:
                ctx.require(
                    beneficiary_id == r.get("authorized_beneficiary_id")
                    and beneficiary_id is not None,
                    "BENEFICIARY_AUTHORIZATION_REQUIRED",
                )
        result = original_challenge(ctx, **args)
        if beneficiary_id:
            ctx.update("xp_b_payment_challenges", result["id"], beneficiary_id=beneficiary_id)
        return result

    def payment(ctx, beneficiary_id=None, **args):
        for r in records(ctx, 41):
            if r["obligation_id"] == args["obligation_id"]:
                c = ctx.record("xp_b_payment_challenges", args["challenge_id"])
                ctx.require(
                    beneficiary_id is not None
                    and beneficiary_id
                    == r.get("authorized_beneficiary_id")
                    == c.get("beneficiary_id"),
                    "BENEFICIARY_CHALLENGE_MISMATCH",
                )
        result = original_payment(ctx, **args)
        if beneficiary_id:
            ctx.update(
                "xp_b_secure_obligations", args["obligation_id"], beneficiary_id=beneficiary_id
            )
        return result

    for name, fn in (
        ("create_payment_challenge", challenge),
        ("execute_authenticated_payment", payment),
    ):
        operations[name]["function"] = fn
        operations[name]["parameters"]["properties"]["beneficiary_id"] = S
        operations[name][
            "description"
        ] += " beneficiary_id is mandatory when a supplier beneficiary record is linked. It binds the authorized routing account in addition to invoice amount and payment credential version."

    def delegated_before(ctx, args):
        for r in records(ctx, -41):
            business = ctx.record("xp_b_managed_businesses", args["business_id"])
            ctx.require(
                r.get("limit_business_id") == business["id"]
                and r.get("payment_limit_minor", 0) >= business["due_minor"]
                and (not r.get("payment_limit_used")),
                "COMPANY_OFFICER_LIMIT_REQUIRED",
            )

    def delegated_after(ctx, args, result):
        for r in records(ctx, -41):
            update(ctx, r, payment_limit_used=True, paid_business_id=args["business_id"])
        return result

    wrap("execute_delegated_payment", delegated_before, delegated_after)
    operations["execute_delegated_payment"][
        "description"
    ] += " A linked company officer amount limit must cover this business and is consumed once."

    def disclosure_after(ctx, args, result):
        for r in records(ctx, -41):
            if r.get("session_active"):
                ctx.update(
                    "xp_b_disclosures",
                    result["id"],
                    session_business_ids=deepcopy(r.get("session_business_ids", [])),
                    session_audience=r.get("report_audience"),
                )
        return result

    wrap("deliver_delegate_data", after=disclosure_after)

    def handle(ctx, e):
        if e["kind"] == "b_enhance_standing_debit":
            r = ctx.record(FAMILIES[42], e["subject_id"])
            if r["standing_status"] == "active":
                available = ctx.record("xp_accounts", r["account_id"])["balance_minor"]
                if available >= r["standing_amount_minor"]:
                    charge(ctx, r, r["standing_amount_minor"], "independent_standing_payment")
                    update(ctx, r, standing_paid_minor=r["standing_amount_minor"])
                else:
                    update(ctx, r, standing_bank_result="insufficient_cash")
            else:
                update(ctx, r, standing_bank_result="suppressed_by_authority_status")
            return True
        if e["kind"].startswith("b_enhance_"):
            payload = e["_payload"]
            r = ctx.record(payload["table"], e["subject_id"])
            fields = deepcopy(payload["fields"])
            ctx.update(payload["table"], r["id"], **fields)
            if e["kind"] == "b_enhance_currency_capacity" and fields["confirmed_shortfall_minor"]:
                ctx.update(
                    "xp_b_settlement_pairs",
                    fields["short_pair_id"],
                    counterparty_ready=False,
                    capacity_shortfall_minor=fields["confirmed_shortfall_minor"],
                )
            if e["kind"] == "b_enhance_tax_appeal":
                claim = ctx.record("xp_b_tax_claims", r["claim_id"])
                amount = fields["additional_credit_minor"]
                ctx.update(
                    "xp_b_tax_claims",
                    claim["id"],
                    approved_minor=claim["approved_minor"] + amount,
                    available_minor=claim["available_minor"] + amount,
                )
            if e["kind"] == "b_enhance_proof_recipient":
                ctx.update(
                    "xp_b_evidence_requests",
                    r["evidence_request_id"],
                    required_fields=fields["required_fields"],
                )
            if e["kind"] == "b_enhance_supplier_cash_refund":
                post(ctx, r, fields["refund_received_minor"], "supplier_credit_cash_refund")
            return True
        return old_event(ctx, e)

    return handle
