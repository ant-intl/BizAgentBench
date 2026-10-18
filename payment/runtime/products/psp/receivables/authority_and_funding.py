"""Payment authority, settlement allocation and supplier fulfillment operations."""

from copy import deepcopy

OPERATIONS = {}
BASE = {}


def op(name, description, **fields):

    def decorate(fn):
        properties = {
            k: {
                "type": t,
                "description": d,
                **({"items": {"type": "string"}} if t == "array" else {}),
            }
            for (k, (t, d)) in fields.items()
        }
        OPERATIONS[name] = dict(
            description=description,
            parameters=dict(
                type="object",
                properties=properties,
                required=list(fields),
                additionalProperties=False,
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return decorate


def row(ctx, table, rid):
    return ctx.record("xp_a_" + table, rid)


def rows(ctx, table, **filters):
    return ctx.records("xp_a_" + table, **filters)


def update(ctx, table, rid, **fields):
    return ctx.update("xp_a_" + table, rid, **fields)


def create(ctx, table, anchor, **fields):
    return ctx.create("xp_a_" + table, customer_id=anchor["customer_id"], **fields)


def cash(ctx, anchor, amount, kind, ref):
    if amount:
        ctx.ledger(anchor["account_id"], amount, anchor.get("currency", "USD"), kind, ref)


@op(
    "a_submit_invoice_authority",
    "Submit a genuine signed payment instruction for bank approval of its scope, version and limit. The stated nonrefundable submission fee is charged. A later bank decision can narrow the requested capacity or beneficiary scope.",
    scope_id=("string", "Commercial payment-authority scope ID."),
    instruction_id=("string", "Signed instruction candidate ID."),
)
def submit_authority(ctx, scope_id, instruction_id):
    scope = row(ctx, "authority_scopes", scope_id)
    instruction = row(ctx, "authority_instructions", instruction_id)
    ctx.require(instruction["scope_id"] == scope_id, "INSTRUCTION_SCOPE_MISMATCH")
    ctx.require(
        BASE["_evidence"](ctx, scope_id, "payment_instruction"),
        "SIGNED_PAYMENT_INSTRUCTION_REQUIRED",
    )
    ctx.require(ctx.minute <= instruction["submit_until"], "INSTRUCTION_SUBMISSION_EXPIRED")
    cash(
        ctx, scope, -instruction["submission_fee_minor"], "AUTHORITY_SUBMISSION_FEE", instruction_id
    )
    application = create(
        ctx,
        "authorizations",
        scope,
        scope_id=scope_id,
        instruction_id=instruction_id,
        status="PENDING",
        requested_limit_minor=instruction["requested_limit_minor"],
        reserved_minor=0,
        consumed_minor=0,
        version=0,
    )
    response = deepcopy(instruction["_bank_response"])
    ctx.schedule(
        "a_authority_decision",
        ctx.minute + instruction["response_delay_minutes"],
        application["id"],
        response=response,
    )
    return application


@op(
    "a_reserve_invoice_authority",
    "Reserve part of an approved authorization for its named commercial scope. The reservation locks that authorization capacity until used, canceled or expired; its fee is nonrefundable. A bank-legal reservation can leave too little capacity for another obligation.",
    authorization_id=("string", "Approved authorization ID."),
    amount_minor=("integer", "Positive authorized-capacity amount in USD minor units."),
    allocation=("string", "INVOICE or DISCRETIONARY use of the reserved capacity."),
)
def reserve_authority(ctx, authorization_id, amount_minor, allocation):
    auth = row(ctx, "authorizations", authorization_id)
    scope = row(ctx, "authority_scopes", auth["scope_id"])
    ctx.require(
        auth["status"] == "APPROVED" and ctx.minute <= auth["valid_until"], "AUTHORITY_NOT_CURRENT"
    )
    ctx.require(
        amount_minor > 0
        and auth["reserved_minor"] + auth["consumed_minor"] + amount_minor
        <= auth["approved_limit_minor"],
        "AUTHORITY_CAPACITY_EXCEEDED",
    )
    ctx.require(allocation in ("INVOICE", "DISCRETIONARY"), "UNKNOWN_CAPACITY_ALLOCATION")
    cash(ctx, scope, -scope["reservation_fee_minor"], "AUTHORITY_RESERVATION_FEE", authorization_id)
    update(
        ctx,
        "authorizations",
        authorization_id,
        reserved_minor=auth["reserved_minor"] + amount_minor,
    )
    return create(
        ctx,
        "authority_reservations",
        scope,
        scope_id=scope["id"],
        authorization_id=authorization_id,
        version=auth["version"],
        amount_minor=amount_minor,
        remaining_minor=amount_minor,
        allocation=allocation,
        expires_minute=min(auth["valid_until"], ctx.minute + scope["reservation_minutes"]),
        status="HELD",
    )


@op(
    "a_cancel_authority_reservation",
    "Release an unused payment-authority capacity reservation. Booking fees are not refunded; consumed payment authority cannot be undone.",
    reservation_id=("string", "Held authorization-capacity reservation ID."),
)
def cancel_authority(ctx, reservation_id):
    reservation = row(ctx, "authority_reservations", reservation_id)
    auth = row(ctx, "authorizations", reservation["authorization_id"])
    ctx.require(reservation["status"] == "HELD", "AUTHORITY_RESERVATION_NOT_HELD")
    update(
        ctx,
        "authorizations",
        auth["id"],
        reserved_minor=auth["reserved_minor"] - reservation["remaining_minor"],
    )
    return update(
        ctx, "authority_reservations", reservation_id, status="CANCELED", remaining_minor=0
    )


@op(
    "a_bind_invoice_instruction",
    "Bind a held invoice-capacity reservation to an approved beneficiary instruction. This establishes the beneficiary mapping used by subsequent bank payments, but does not itself pay an invoice. The approved invoice scope and recipient mapping are preserved in the binding.",
    reservation_id=("string", "Held INVOICE authority reservation ID."),
    beneficiary_id=(
        "string",
        "Approved beneficiary identifier; a shop ID or recipient instruction ID depending on the scope.",
    ),
)
def bind_instruction(ctx, reservation_id, beneficiary_id):
    reservation = row(ctx, "authority_reservations", reservation_id)
    auth = row(ctx, "authorizations", reservation["authorization_id"])
    scope = row(ctx, "authority_scopes", reservation["scope_id"])
    ctx.require(
        reservation["status"] == "HELD"
        and reservation["allocation"] == "INVOICE"
        and (ctx.minute <= reservation["expires_minute"]),
        "INVOICE_RESERVATION_REQUIRED",
    )
    ctx.require(
        auth["version"] == reservation["version"] and beneficiary_id in auth["beneficiary_ids"],
        "BENEFICIARY_NOT_AUTHORIZED",
    )
    return create(
        ctx,
        "instruction_bindings",
        scope,
        scope_id=scope["id"],
        reservation_id=reservation_id,
        authorization_id=auth["id"],
        authority_version=auth["version"],
        beneficiary_id=beneficiary_id,
        invoice_ids=deepcopy(auth["invoice_ids"]),
        status="ACTIVE",
        created_minute=ctx.minute,
    )


def consume_authority(ctx, subject, amount, beneficiary):
    scope_id = subject.get("authority_scope_id")
    if not scope_id:
        return {}
    scope = row(ctx, "authority_scopes", scope_id)
    bindings = [
        b
        for b in rows(ctx, "instruction_bindings", scope_id=scope_id, status="ACTIVE")
        if b["beneficiary_id"] == beneficiary and subject["id"] in b["invoice_ids"]
    ]
    for binding in bindings:
        reservation = row(ctx, "authority_reservations", binding["reservation_id"])
        auth = row(ctx, "authorizations", binding["authorization_id"])
        if (
            reservation["status"] == "HELD"
            and reservation["remaining_minor"] >= amount
            and (ctx.minute <= reservation["expires_minute"])
            and (auth["status"] == "APPROVED")
            and (binding["authority_version"] == auth["version"])
        ):
            remaining = reservation["remaining_minor"] - amount
            update(
                ctx,
                "authority_reservations",
                reservation["id"],
                remaining_minor=remaining,
                status="CONSUMED" if not remaining else "HELD",
            )
            update(
                ctx,
                "authorizations",
                auth["id"],
                reserved_minor=auth["reserved_minor"] - amount,
                consumed_minor=auth["consumed_minor"] + amount,
            )
            return dict(
                instruction_binding_id=binding["id"],
                authorization_id=auth["id"],
                authorization_version=auth["version"],
                authority_consumed_minor=amount,
                authorized_beneficiary_id=beneficiary,
            )
    ctx.require(False, "NO_CURRENT_INVOICE_AUTHORITY")


@op(
    "a_request_beneficiary_allocation",
    "Ask the receiving bank or merchant to identify the received value and its commercial allocation. The request is tied to an actual settled source; it is not another payment. The fixed response can disclose deductions, wrong-account allocations or unavailable value.",
    settlement_id=("string", "Beneficiary settlement-application case ID."),
)
def request_allocation(ctx, settlement_id):
    settlement = row(ctx, "settlement_cases", settlement_id)
    ctx.require(settlement["status"] == "OPEN", "BENEFICIARY_CASE_ALREADY_REQUESTED")
    source = ctx.record(settlement["source_collection"], settlement["source_id"])
    field = settlement["source_status_field"]
    ctx.require(
        source.get(field) in settlement["source_final_values"], "SOURCE_SETTLEMENT_NOT_FINAL"
    )
    cash(
        ctx, settlement, -settlement["inquiry_fee_minor"], "BENEFICIARY_INQUIRY_FEE", settlement_id
    )
    ctx.schedule(
        "a_beneficiary_allocation_result",
        ctx.minute + settlement["response_delay_minutes"],
        settlement_id,
        response=deepcopy(settlement["_bank_response"]),
    )
    return update(
        ctx, "settlement_cases", settlement_id, status="REQUESTED", source_snapshot=deepcopy(source)
    )


@op(
    "a_reassign_beneficiary_credit",
    "Move unapplied beneficiary value between the published commercial accounts. Reassignment is an irrevocable allocation instruction and charges the published fee; moving money to another valid account can leave the intended invoice unpaid.",
    settlement_id=("string", "Reviewed beneficiary settlement case ID."),
    destination_account=(
        "string",
        "One of the published beneficiary commercial-account references.",
    ),
    amount_minor=("integer", "Positive unapplied value to reassign in minor units."),
)
def reassign_credit(ctx, settlement_id, destination_account, amount_minor):
    case = row(ctx, "settlement_cases", settlement_id)
    ctx.require(
        case["status"] == "REVIEWED" and destination_account in case["commercial_accounts"],
        "ALLOCATION_ACCOUNT_UNKNOWN",
    )
    ctx.require(0 < amount_minor <= case["unapplied_minor"], "UNAPPLIED_VALUE_INSUFFICIENT")
    cash(ctx, case, -case["reassignment_fee_minor"], "BENEFICIARY_REASSIGNMENT_FEE", settlement_id)
    allocations = deepcopy(case.get("allocations", {}))
    allocations[destination_account] = allocations.get(destination_account, 0) + amount_minor
    return update(
        ctx,
        "settlement_cases",
        settlement_id,
        unapplied_minor=case["unapplied_minor"] - amount_minor,
        allocations=allocations,
    )


@op(
    "a_resolve_beneficiary_deduction",
    "Resolve an observed deduction using an eligible bank reimbursement or a sender-funded fee top-up. A bank reimbursement may be directed to the beneficiary or refunded to the sender, consuming it once. All fee top-ups debit real cash.",
    settlement_id=("string", "Reviewed beneficiary settlement case ID."),
    method=("string", "BANK_TO_BENEFICIARY, BANK_TO_SENDER, or SENDER_TOPUP."),
    amount_minor=("integer", "Deduction value being resolved in minor units."),
)
def resolve_deduction(ctx, settlement_id, method, amount_minor):
    case = row(ctx, "settlement_cases", settlement_id)
    ctx.require(case["status"] == "REVIEWED" and amount_minor > 0, "REVIEWED_DEDUCTION_REQUIRED")
    cash(ctx, case, -case["resolution_fee_minor"], "BENEFICIARY_RESOLUTION_FEE", settlement_id)
    ctx.require(
        method in ("BANK_TO_BENEFICIARY", "BANK_TO_SENDER", "SENDER_TOPUP"),
        "DEDUCTION_METHOD_UNKNOWN",
    )
    if method.startswith("BANK_"):
        ctx.require(
            ctx.minute <= case["reimbursement_until"]
            and amount_minor <= case["bank_reimbursement_remaining_minor"],
            "BANK_REIMBURSEMENT_UNAVAILABLE",
        )
        update(
            ctx,
            "settlement_cases",
            settlement_id,
            bank_reimbursement_remaining_minor=case["bank_reimbursement_remaining_minor"]
            - amount_minor,
        )
        if method == "BANK_TO_SENDER":
            ctx.ledger(
                case["clearing_account_id"],
                -amount_minor,
                "USD",
                "BENEFICIARY_RESERVE_RETURN",
                settlement_id,
            )
            cash(ctx, case, amount_minor, "BENEFICIARY_FEE_REFUND", settlement_id)
    else:
        cash(ctx, case, -amount_minor, "BENEFICIARY_FEE_TOPUP", settlement_id)
        ctx.ledger(
            case["clearing_account_id"],
            amount_minor,
            "USD",
            "BENEFICIARY_RESERVE_TOPUP",
            settlement_id,
        )
    if method != "BANK_TO_SENDER":
        update(
            ctx,
            "settlement_cases",
            settlement_id,
            unapplied_minor=case["unapplied_minor"] + amount_minor,
        )
    return create(
        ctx,
        "deduction_resolutions",
        case,
        settlement_id=settlement_id,
        method=method,
        amount_minor=amount_minor,
        minute=ctx.minute,
    )


@op(
    "a_apply_beneficiary_invoice",
    "Apply allocated beneficiary value to a published receivable. This consumes value once and creates the merchant or receiving-bank application receipt; a settled sending-bank payment alone is not this commercial receipt.",
    settlement_id=("string", "Reviewed settlement case ID."),
    commercial_account=(
        "string",
        "Beneficiary account from which allocated value will be applied.",
    ),
    receivable_id=("string", "Published beneficiary receivable ID."),
    amount_minor=("integer", "Positive receivable amount in minor units."),
)
def apply_beneficiary_invoice(ctx, settlement_id, commercial_account, receivable_id, amount_minor):
    case = row(ctx, "settlement_cases", settlement_id)
    receivable = row(ctx, "beneficiary_receivables", receivable_id)
    ctx.require(
        case["status"] == "REVIEWED" and receivable["settlement_id"] == settlement_id,
        "RECEIVABLE_SETTLEMENT_MISMATCH",
    )
    ctx.require(
        commercial_account == receivable["commercial_account"] and amount_minor > 0,
        "RECEIVABLE_ACCOUNT_MISMATCH",
    )
    allocations = deepcopy(case.get("allocations", {}))
    ctx.require(
        allocations.get(commercial_account, 0) >= amount_minor,
        "BENEFICIARY_ALLOCATED_VALUE_INSUFFICIENT",
    )
    allocations[commercial_account] -= amount_minor
    update(ctx, "settlement_cases", settlement_id, allocations=allocations)
    update(
        ctx,
        "beneficiary_receivables",
        receivable_id,
        applied_minor=receivable.get("applied_minor", 0) + amount_minor,
    )
    ctx.ledger(
        case["clearing_account_id"],
        -amount_minor,
        "USD",
        "BENEFICIARY_APPLICATION_RELEASE",
        receivable_id,
    )
    if (
        case["flow_direction"] == "INCOMING"
        and receivable["commercial_reference"] == case["commercial_reference"]
    ):
        cash(ctx, case, amount_minor, "BENEFICIARY_CREDIT_AVAILABLE", receivable_id)
    matched = sum(
        (
            r.get("applied_minor", 0)
            for r in rows(ctx, "beneficiary_receivables", settlement_id=settlement_id)
            if r["commercial_reference"] == case["commercial_reference"]
        )
    )
    if matched == case["principal_minor"]:
        ctx.update(
            case["source_collection"],
            case["source_id"],
            commercial_application_complete=True,
            **{"status": "CREDITED"} if case.get("restore_credited_status") else {},
        )
    return create(
        ctx,
        "beneficiary_applications",
        case,
        settlement_id=settlement_id,
        receivable_id=receivable_id,
        commercial_account=commercial_account,
        amount_minor=amount_minor,
        source_id=case["source_id"],
        minute=ctx.minute,
    )


@op(
    "a_book_supplier_dispatch",
    "Book a paid purchase into a supplier dispatch route. The route fixes product compatibility, nonrefundable transport cost and delivery window. The supplier later publishes actual allocated units and eligible shortfall refund value.",
    delivery_id=("string", "Paid purchase fulfillment record ID."),
    transport_id=("string", "Published supplier transport route ID."),
)
def book_dispatch(ctx, delivery_id, transport_id):
    delivery = row(ctx, "deliveries", delivery_id)
    transport = row(ctx, "transport_routes", transport_id)
    source = ctx.record(delivery["source_collection"], delivery["source_id"])
    ctx.require(
        source.get(delivery["source_status_field"]) in delivery["source_paid_values"],
        "PURCHASE_NOT_PAID",
    )
    ctx.require(
        delivery["status"] == "UNBOOKED"
        and transport["delivery_id"] == delivery_id
        and (ctx.minute <= transport["book_until"]),
        "DISPATCH_NOT_BOOKABLE",
    )
    cash(ctx, delivery, -transport["fee_minor"], "SUPPLIER_TRANSPORT_FEE", delivery_id)
    response = deepcopy(delivery["_dispatch_response"])
    if delivery.get("source_compatibility_field"):
        update(
            ctx,
            "deliveries",
            delivery_id,
            required_compatibility=source[delivery["source_compatibility_field"]],
        )
        if response["delivered_quantity"] < delivery["required_quantity"]:
            reservations = [
                row(ctx, "reservations", rid) for rid in source["committed_reservation_ids"]
            ]
            response["shortfall_refund_minor"] = next(
                (
                    r["amount_minor"]
                    for r in reservations
                    if r["component"] == delivery["refund_component"]
                )
            )
    ctx.schedule(
        "a_supplier_dispatch_result",
        ctx.minute + transport["transit_minutes"],
        delivery_id,
        response=response,
    )
    return update(
        ctx,
        "deliveries",
        delivery_id,
        status="DISPATCHED",
        transport_id=transport_id,
        booked_minute=ctx.minute,
        promised_compatibility=transport["compatibility"],
    )


@op(
    "a_claim_supplier_shortfall",
    "Claim the supplier-confirmed undelivered value. CASH restores spendable bank funds; STORE_CREDIT creates restricted replacement credit accepted only by matching supplier offers. This is a one-time disposition of the actual published shortfall.",
    delivery_id=("string", "Fulfillment record with a published supplier shortfall."),
    disposition=("string", "CASH or STORE_CREDIT."),
)
def claim_shortfall(ctx, delivery_id, disposition):
    delivery = row(ctx, "deliveries", delivery_id)
    ctx.require(
        delivery["status"] == "RECEIVED"
        and delivery["shortfall_refund_minor"] > 0
        and (not delivery.get("refund_claimed")),
        "NO_UNCLAIMED_SUPPLIER_SHORTFALL",
    )
    ctx.require(disposition in ("CASH", "STORE_CREDIT"), "REFUND_DISPOSITION_UNKNOWN")
    if disposition == "CASH":
        cash(
            ctx,
            delivery,
            delivery["shortfall_refund_minor"],
            "SUPPLIER_SHORTFALL_REFUND",
            delivery_id,
        )
    return update(
        ctx,
        "deliveries",
        delivery_id,
        refund_claimed=True,
        refund_disposition=disposition,
        store_credit_minor=(
            delivery["shortfall_refund_minor"] if disposition == "STORE_CREDIT" else 0
        ),
    )


@op(
    "a_order_replacement_units",
    "Purchase replacement units under a published offer. Compatibility, available quantity, expiry and accepted refund-credit type are public. A technically valid replacement can be incompatible with the customer's original purchase.",
    delivery_id=("string", "Fulfillment record with remaining undelivered units."),
    offer_id=("string", "Replacement supply offer ID."),
    quantity=("integer", "Positive integer replacement-unit quantity."),
    funding=("string", "CASH or STORE_CREDIT."),
)
def order_replacement(ctx, delivery_id, offer_id, quantity, funding):
    delivery = row(ctx, "deliveries", delivery_id)
    offer = row(ctx, "replacement_offers", offer_id)
    ctx.require(
        delivery["status"] == "RECEIVED"
        and offer["delivery_id"] == delivery_id
        and (quantity > 0)
        and (quantity <= offer["available_quantity"])
        and (ctx.minute <= offer["valid_until"]),
        "REPLACEMENT_NOT_AVAILABLE",
    )
    ctx.require(funding in offer["funding_methods"], "REPLACEMENT_FUNDING_UNSUPPORTED")
    cost = quantity * offer["unit_price_minor"] + offer["fee_minor"]
    if funding == "CASH":
        cash(ctx, delivery, -cost, "SUPPLIER_REPLACEMENT_PURCHASE", offer_id)
    else:
        ctx.require(delivery.get("store_credit_minor", 0) >= cost, "STORE_CREDIT_INSUFFICIENT")
        update(
            ctx, "deliveries", delivery_id, store_credit_minor=delivery["store_credit_minor"] - cost
        )
    update(
        ctx,
        "replacement_offers",
        offer_id,
        available_quantity=offer["available_quantity"] - quantity,
    )
    return create(
        ctx,
        "replacement_orders",
        delivery,
        delivery_id=delivery_id,
        offer_id=offer_id,
        quantity=quantity,
        compatibility=offer["compatibility"],
        amount_minor=cost,
        funding=funding,
        status="DELIVERED",
        minute=ctx.minute,
    )


@op(
    "a_accept_supplier_delivery",
    "Record actual delivered original and replacement units against the commercial purchase. The receiving service reports their quantity and compatibility; it does not waive the customer's required specification or budget.",
    delivery_id=("string", "Supplier fulfillment record ID."),
    replacement_order_ids=(
        "array",
        "Distinct actually delivered replacement-order IDs; empty when original supply was complete.",
    ),
)
def accept_delivery(ctx, delivery_id, replacement_order_ids):
    delivery = row(ctx, "deliveries", delivery_id)
    ctx.require(
        delivery["status"] == "RECEIVED"
        and len(set(replacement_order_ids)) == len(replacement_order_ids),
        "DELIVERY_RECEIPT_REQUIRED",
    )
    replacements = [row(ctx, "replacement_orders", r) for r in replacement_order_ids]
    ctx.require(
        all((r["delivery_id"] == delivery_id and r["status"] == "DELIVERED" for r in replacements)),
        "REPLACEMENT_DELIVERY_MISMATCH",
    )
    return update(
        ctx,
        "deliveries",
        delivery_id,
        status="ACCEPTED",
        accepted_quantity=delivery["delivered_quantity"]
        + sum((r["quantity"] for r in replacements)),
        accepted_compatibilities=[delivery["promised_compatibility"]]
        + [r["compatibility"] for r in replacements],
        replacement_order_ids=replacement_order_ids,
        accepted_minute=ctx.minute,
    )


def apply_event(ctx, event):
    kind = event["kind"]
    response = event.get("_payload", {}).get("response", {})
    if kind == "a_authority_decision":
        update(ctx, "authorizations", event["subject_id"], **deepcopy(response))
    elif kind == "a_beneficiary_allocation_result":
        update(
            ctx, "settlement_cases", event["subject_id"], status="REVIEWED", **deepcopy(response)
        )
    elif kind == "a_supplier_dispatch_result":
        update(ctx, "deliveries", event["subject_id"], status="RECEIVED", **deepcopy(response))
    else:
        return False
    ctx.emit(kind, subject_id=event["subject_id"], published_response=response)
    return True


def register(namespace):
    BASE.update({k: namespace.helper(k) for k in ("_evidence",)})
    namespace.operations.update(OPERATIONS)
    for name, table, key in [
        ("a_amend_transfer_instruction", "xp_a_incoming", "incoming_id"),
        ("a_ingest_credit_notice", "xp_a_notices", "notice_id"),
    ]:
        original = namespace.operations[name]["function"]

        def wrapped(ctx, _original=original, _table=table, _key=key, **args):
            result = _original(ctx, **args)
            source = ctx.record(_table, args[_key])
            if _table == "xp_a_notices":
                source = ctx.record("xp_a_bank_transactions", source["bank_transaction_id"])
            hold_source(ctx, source)
            return result

        namespace.operations[name]["function"] = wrapped


def hold_source(ctx, source):
    sid = source.get("settlement_case_id")
    if not sid:
        return
    case = row(ctx, "settlement_cases", sid)
    if case.get("funded"):
        return
    amount = case["principal_minor"]
    if case["flow_direction"] == "INCOMING":
        cash(ctx, case, -amount, "BENEFICIARY_CREDIT_PENDING", sid)
    ctx.ledger(case["clearing_account_id"], amount, "USD", "BENEFICIARY_CLEARING_RECEIPT", sid)
    update(ctx, "settlement_cases", sid, funded=True)
    if case.get("restore_credited_status"):
        ctx.update(case["source_collection"], case["source_id"], status="AWAITING_APPLICATION")
