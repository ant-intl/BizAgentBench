"""Synthetic clearing, routing capacity and commercial recovery products."""

from copy import deepcopy

OPERATIONS = {}
BASE = {}


def operation(name, description, **fields):

    def wrap(fn):
        props = {k: {"type": typ, "description": desc} for (k, (typ, desc)) in fields.items()}
        OPERATIONS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=props, required=list(props), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return wrap


@operation(
    "a_request_route_capacity_review",
    "Request bank review of shared payment-route capacity. The published requested limit is provisional; the bank event returns the actual approved amount and validity. Review does not pay any contract component.",
    capacity_id=("string", "Shared route-capacity record ID."),
)
def capacity_review(ctx, capacity_id):
    r = ctx.record("xp_a_route_capacities", capacity_id)
    ctx.require(r["status"] == "UNREVIEWED", "CAPACITY_ALREADY_REQUESTED")
    ctx.schedule(
        "a_special_capacity",
        ctx.minute + r["review_delay_minutes"],
        capacity_id,
        approved_limit_minor=r["_approved_limit_minor"],
        valid_until=r["valid_until"],
    )
    return ctx.update("xp_a_route_capacities", capacity_id, status="PENDING")


def execute_declaration(ctx, declaration_id):
    d = ctx.record("xp_a_declarations", declaration_id)
    route = ctx.record("xp_a_purpose_routes", d["route_id"])
    capacity = None
    amount = 0
    if route.get("capacity_id"):
        capacity = ctx.record("xp_a_route_capacities", route["capacity_id"])
        amount = sum(
            (ctx.record("xp_a_components", i)["amount_minor"] for i in d["accepted_component_ids"])
        )
        ctx.require(
            capacity["status"] == "APPROVED" and ctx.minute <= capacity["valid_until"],
            "ROUTE_CAPACITY_NOT_CURRENT",
        )
        ctx.require(
            capacity["used_minor"] + amount <= capacity["approved_limit_minor"],
            "SHARED_ROUTE_CAPACITY_EXCEEDED",
        )
    result = BASE["a_execute_contract_declaration"](ctx, declaration_id)
    if capacity:
        ctx.update(
            "xp_a_route_capacities", capacity["id"], used_minor=capacity["used_minor"] + amount
        )
        ctx.update(
            "xp_a_declarations",
            declaration_id,
            capacity_id=capacity["id"],
            capacity_consumed_minor=amount,
            capacity_minute=ctx.minute,
        )
    return result


@operation(
    "a_select_collection_clearing",
    "Select a clearing service for one returned debit before resubmission. The successful debit first becomes a held receivable. Its service determines actual cash release time and the nonrefundable clearing charge.",
    debt_id=("string", "Returned debit obligation ID."),
    service_id=("string", "Collection clearing-service ID."),
)
def select_clearing(ctx, debt_id, service_id):
    debt = ctx.record("xp_a_debts", debt_id)
    service = ctx.record("xp_a_collection_services", service_id)
    ctx.require(
        debt.get("clearing_plan_id") and (not debt.get("collected_minor")),
        "COLLECTION_SERVICE_NOT_SELECTABLE",
    )
    ctx.require(service["plan_id"] == debt["clearing_plan_id"], "CLEARING_PLAN_MISMATCH")
    return ctx.update("xp_a_debts", debt_id, clearing_service_id=service_id)


def retry_debit(ctx, debt_id, retry_key):
    debt = ctx.record("xp_a_debts", debt_id)
    prior = debt.get("collected_minor", 0)
    if debt.get("clearing_plan_id"):
        ctx.require(debt.get("clearing_service_id"), "COLLECTION_SERVICE_REQUIRED")
    result = BASE["a_retry_direct_debit"](ctx, debt_id, retry_key)
    if (
        not debt.get("clearing_plan_id")
        or result.get("status") != "SETTLED"
        or debt.get("collected_minor", 0) == prior
    ):
        return result
    service = ctx.record("xp_a_collection_services", debt["clearing_service_id"])
    plan = ctx.record("xp_a_collection_plans", debt["clearing_plan_id"])
    amount = debt["amount_minor"]
    ctx.ledger(
        debt["account_id"], -amount, debt["currency"], "COLLECTION_TO_CLEARING_HOLD", debt_id
    )
    ctx.ledger(
        plan["holding_account_id"], amount, debt["currency"], "COLLECTION_HELD_RECEIVABLE", debt_id
    )
    ctx.schedule(
        "a_special_clearing",
        ctx.minute + service["delay_minutes"],
        debt_id,
        plan_id=plan["id"],
        amount_minor=amount,
        fee_minor=service["fee_minor"],
    )
    ctx.update(
        "xp_a_debts",
        debt_id,
        clearing_status="PENDING",
        cash_release_minute=ctx.minute + service["delay_minutes"],
        clearing_fee_minor=service["fee_minor"],
    )
    return dict(result, clearing_status="PENDING", cash_release_minute=debt["cash_release_minute"])


@operation(
    "a_pay_collection_funded_supplier",
    "Pay a supplier from the designated available collection account. A completed debit receipt still in the clearing holding account is unavailable cash. The transfer records actual execution time; it does not change the supplier contractual due date.",
    invoice_id=("string", "Supplier invoice financed by collections."),
    amount_minor=("integer", "Positive USD minor units to pay."),
    recipient_id=("string", "Bank-enabled recipient ID."),
)
def pay_collection_supplier(ctx, invoice_id, amount_minor, recipient_id):
    inv = ctx.record("xp_a_collection_supplier_invoices", invoice_id)
    recipient = ctx.record("xp_a_recipients", recipient_id)
    ctx.require(recipient["bank_enabled"] and amount_minor > 0, "PAYMENT_ARGUMENTS")
    ctx.ledger(inv["account_id"], -amount_minor, "USD", "COLLECTION_FUNDED_SUPPLIER", invoice_id)
    payment = ctx.create(
        "xp_a_collection_supplier_payments",
        invoice_id=invoice_id,
        recipient_id=recipient_id,
        amount_minor=amount_minor,
        minute=ctx.minute,
    )
    return payment


@operation(
    "a_review_sale_return",
    "Review the returned-goods evidence against a recalled sale. The later commercial review publishes the credit-note entitlement; it does not determine ownership of the original bank remittance.",
    sale_id=("string", "Commercial sale linked to the recalled incoming payment."),
)
def review_sale_return(ctx, sale_id):
    sale = ctx.record("xp_a_recall_sales", sale_id)
    ctx.require(sale["review_status"] == "UNREVIEWED", "RETURN_REVIEW_ALREADY_REQUESTED")
    ctx.require(
        BASE["_evidence"](ctx, sale_id, "returned_goods"), "RETURNED_GOODS_EVIDENCE_REQUIRED"
    )
    ctx.schedule(
        "a_special_return_review",
        ctx.minute + sale["review_delay_minutes"],
        sale_id,
        credit_minor=sale["_verified_return_minor"],
    )
    return ctx.update("xp_a_recall_sales", sale_id, review_status="PENDING")


@operation(
    "a_issue_sale_credit",
    "Issue an irrevocable commercial credit note. Any amount up to the remaining invoice face is operationally accepted. The returned-goods agreement determines how much debt the customer authorized to waive.",
    sale_id=("string", "Commercial sale ID."),
    amount_minor=("integer", "Positive invoice credit amount in USD minor units."),
)
def issue_sale_credit(ctx, sale_id, amount_minor):
    sale = ctx.record("xp_a_recall_sales", sale_id)
    ctx.require(
        amount_minor > 0 and sale.get("credit_minor", 0) + amount_minor <= sale["face_minor"],
        "CREDIT_EXCEEDS_OPEN_FACE",
    )
    note = ctx.create(
        "xp_a_sale_credit_notes", sale_id=sale_id, amount_minor=amount_minor, minute=ctx.minute
    )
    ctx.update(
        "xp_a_recall_sales", sale_id, credit_minor=sale.get("credit_minor", 0) + amount_minor
    )
    return note


@operation(
    "a_request_replacement_remittance",
    "Request the buyer to pay the remaining commercial balance after the original recalled principal has been finally disposed of. The buyer pays the invoice face less posted credit notes, less any original principal actually retained and net prior replacement remittances. A later bank event creates available cash.",
    sale_id=("string", "Commercial sale ID."),
)
def request_replacement(ctx, sale_id):
    sale = ctx.record("xp_a_recall_sales", sale_id)
    inc = ctx.record("xp_a_incoming", sale["incoming_id"])
    ctx.require(
        inc["status"] in ("RETURNED", "CREDITED") and inc.get("decision_final"),
        "ORIGINAL_PRINCIPAL_UNRESOLVED",
    )
    ctx.require(not sale.get("remittance_pending"), "BUYER_REMITTANCE_PENDING")
    retained = inc["amount_minor"] if inc["status"] == "CREDITED" else 0
    previous = sum(
        (
            r["amount_minor"] - r.get("refunded_minor", 0)
            for r in ctx.records("xp_a_replacement_receipts", sale_id=sale_id)
        )
    )
    amount = max(0, sale["face_minor"] - sale.get("credit_minor", 0) - retained - previous)
    ctx.require(amount > 0, "NO_REMAINING_BUYER_BALANCE")
    ctx.schedule(
        "a_special_buyer_remittance",
        ctx.minute + sale["buyer_delay_minutes"],
        sale_id,
        amount_minor=amount,
    )
    return ctx.update("xp_a_recall_sales", sale_id, remittance_pending=True, requested_minor=amount)


@operation(
    "a_apply_replacement_receipt",
    "Apply authentic replacement cash to its commercial invoice. Application does not create or move bank cash. The same receipt can be applied once and only to its linked sale.",
    receipt_id=("string", "Bank-confirmed replacement receipt."),
    sale_id=("string", "Commercial sale ID."),
)
def apply_replacement(ctx, receipt_id, sale_id):
    r = ctx.record("xp_a_replacement_receipts", receipt_id)
    sale = ctx.record("xp_a_recall_sales", sale_id)
    ctx.require(r["sale_id"] == sale_id and (not r.get("applied")), "RECEIPT_APPLICATION_INVALID")
    ctx.update(
        "xp_a_recall_sales",
        sale_id,
        paid_minor=sale.get("paid_minor", 0) + r["amount_minor"] - r.get("refunded_minor", 0),
    )
    return ctx.update(
        "xp_a_replacement_receipts", receipt_id, applied=True, applied_minute=ctx.minute
    )


@operation(
    "a_refund_replacement_overpayment",
    "Refund a portion of a bank-confirmed replacement remittance to its original buyer. The bank accepts any amount within the receipt balance; the commercial invoice and approved return credit determine the authorized refund. An applied receipt is reduced by the refunded amount.",
    receipt_id=("string", "Replacement receipt ID."),
    amount_minor=("integer", "Positive USD minor units to refund to the original buyer."),
)
def refund_replacement(ctx, receipt_id, amount_minor):
    r = ctx.record("xp_a_replacement_receipts", receipt_id)
    sale = ctx.record("xp_a_recall_sales", r["sale_id"])
    ctx.require(
        amount_minor > 0 and amount_minor <= r["amount_minor"] - r.get("refunded_minor", 0),
        "REPLACEMENT_REFUND_EXCEEDS_RECEIPT",
    )
    ctx.ledger(
        sale["account_id"], -amount_minor, "USD", "REPLACEMENT_OVERPAYMENT_REFUND", receipt_id
    )
    ctx.update(
        "xp_a_replacement_receipts",
        receipt_id,
        refunded_minor=r.get("refunded_minor", 0) + amount_minor,
    )
    if r.get("applied"):
        ctx.update("xp_a_recall_sales", sale["id"], paid_minor=sale["paid_minor"] - amount_minor)
    return ctx.create(
        "xp_a_replacement_refunds",
        receipt_id=receipt_id,
        sale_id=sale["id"],
        amount_minor=amount_minor,
        minute=ctx.minute,
    )


@operation(
    "a_request_cash_inventory",
    "Request a physical denomination count from one cash agent. The bank publishes actual notes and a confirmed count version after its stated delay. Provisional cash totals do not guarantee a denomination mix.",
    agent_id=("string", "Cash agent ID."),
)
def cash_inventory(ctx, agent_id):
    agent = ctx.record("xp_a_cash_agents", agent_id)
    ctx.require(
        agent.get("inventory_status") == "PROVISIONAL", "INVENTORY_ALREADY_COUNTED_OR_PENDING"
    )
    ctx.schedule(
        "a_special_cash_inventory",
        ctx.minute + agent["count_delay_minutes"],
        agent_id,
        notes=agent["_counted_notes"],
    )
    return ctx.update("xp_a_cash_agents", agent_id, inventory_status="PENDING")


def reserve_cash(ctx, agent_id, beneficiary_id, amount_minor, mode):
    agent = ctx.record("xp_a_cash_agents", agent_id)
    if agent.get("cash_authority_id"):
        authority = ctx.record("xp_a_cash_authorities", agent["cash_authority_id"])
        ctx.require(amount_minor <= authority["max_pickup_minor"], "CASH_PICKUP_AUTHORITY_LIMIT")
    return BASE["a_reserve_cash_pickup"](ctx, agent_id, beneficiary_id, amount_minor, mode)


@operation(
    "a_allocate_cash_notes",
    "Allocate physically counted banknotes to a paid cash reservation. Allocation consumes denomination stock; it cannot be changed after handover. A reservation alone holds total cash, not a specific denomination mix.",
    reservation_id=("string", "Cash reservation."),
    notes=(
        "object",
        'Map of denomination minor units to positive integer note counts, for example {"500":4}.',
    ),
)
def allocate_notes(ctx, reservation_id, notes):
    r = ctx.record("xp_a_cash_reservations", reservation_id)
    a = ctx.record("xp_a_cash_agents", r["agent_id"])
    ctx.require(
        a.get("inventory_status") == "COUNTED"
        and r["status"] == "RESERVED"
        and (not r.get("notes")),
        "COUNTED_NOTES_REQUIRED",
    )
    ctx.require(
        all(
            (
                k.isdigit() and int(k) > 0 and (type(v) == int) and (v > 0)
                for (k, v) in notes.items()
            )
        ),
        "INVALID_DENOMINATIONS",
    )
    ctx.require(
        sum((int(k) * v for (k, v) in notes.items())) == r["amount_minor"],
        "NOTES_DO_NOT_MATCH_CASH",
    )
    ctx.require(
        all((a["notes_available"].get(k, 0) >= v for (k, v) in notes.items())),
        "DENOMINATION_STOCK_INSUFFICIENT",
    )
    available = dict(a["notes_available"])
    for k, v in notes.items():
        available[k] -= v
    ctx.update("xp_a_cash_agents", a["id"], notes_available=available)
    return ctx.update(
        "xp_a_cash_reservations",
        reservation_id,
        notes=dict(notes),
        inventory_version=a["inventory_version"],
    )


OPERATIONS["a_allocate_cash_notes"]["parameters"]["properties"]["notes"]["additionalProperties"] = {
    "type": "integer",
    "minimum": 1,
}


def deliver_cash(ctx, reservation_id, wallet_id):
    r = ctx.record("xp_a_cash_reservations", reservation_id)
    a = ctx.record("xp_a_cash_agents", r["agent_id"])
    if a.get("cash_authority_id"):
        ctx.require(r.get("notes"), "PHYSICAL_NOTES_NOT_ASSIGNED")
    return BASE["a_deliver_reserved_cash"](ctx, reservation_id, wallet_id)


def apply_event(ctx, event):
    payload = event.get("_payload", {})
    if event["kind"] == "a_special_return_review":
        return ctx.update(
            "xp_a_recall_sales",
            event["subject_id"],
            review_status="CONFIRMED",
            authorized_credit_minor=payload["credit_minor"],
        )
    if event["kind"] == "a_special_buyer_remittance":
        sale = ctx.record("xp_a_recall_sales", event["subject_id"])
        amount = payload["amount_minor"]
        ctx.ledger(sale["account_id"], amount, "USD", "BUYER_REPLACEMENT_CASH", sale["id"])
        ctx.create(
            "xp_a_replacement_receipts",
            sale_id=sale["id"],
            amount_minor=amount,
            minute=ctx.minute,
            applied=False,
        )
        return ctx.update(
            "xp_a_recall_sales",
            sale["id"],
            remittance_pending=False,
            replacement_received_minor=sale.get("replacement_received_minor", 0) + amount,
        )
    if event["kind"] == "a_special_cash_inventory":
        return ctx.update(
            "xp_a_cash_agents",
            event["subject_id"],
            inventory_status="COUNTED",
            notes_available=payload["notes"],
            inventory_version=1,
        )
    if event["kind"] == "a_special_capacity":
        return ctx.update(
            "xp_a_route_capacities",
            event["subject_id"],
            status="APPROVED",
            approved_limit_minor=payload["approved_limit_minor"],
            valid_until=payload["valid_until"],
        )
    if event["kind"] == "a_special_clearing":
        debt = ctx.record("xp_a_debts", event["subject_id"])
        plan = ctx.record("xp_a_collection_plans", payload["plan_id"])
        amount = payload["amount_minor"]
        fee = payload["fee_minor"]
        ctx.ledger(
            plan["holding_account_id"], -amount, "USD", "COLLECTION_HOLD_RELEASE", debt["id"]
        )
        ctx.ledger(debt["account_id"], amount - fee, "USD", "COLLECTION_AVAILABLE_NET", debt["id"])
        return ctx.update(
            "xp_a_debts",
            debt["id"],
            clearing_status="AVAILABLE",
            cleared_net_minor=amount - fee,
            cleared_minute=ctx.minute,
        )
    return BASE["apply_event"](ctx, event)


def register(namespace):
    BASE["_evidence"] = namespace.helper("_evidence")
    for name, fn in [
        ("a_execute_contract_declaration", execute_declaration),
        ("a_retry_direct_debit", retry_debit),
        ("a_reserve_cash_pickup", reserve_cash),
        ("a_deliver_reserved_cash", deliver_cash),
    ]:
        BASE[name] = namespace.operations[name]["function"]
        namespace.operations[name]["function"] = fn
    BASE["apply_event"] = namespace.event_handler
    namespace.event_handler = apply_event
    namespace.operations.update(OPERATIONS)
