"""Scoped settlement calendars, supplier performance, collection ownership and cash itineraries."""

from copy import deepcopy

OPS = {}
BASE = {}


def operation(name, description, **fields):

    def wrap(fn):
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object",
                properties={k: dict(type=t, description=d) for (k, (t, d)) in fields.items()},
                required=list(fields),
                additionalProperties=False,
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return wrap


def rows(ctx, table):
    return ctx.records("xp_a_" + table)


def row(ctx, table, rid):
    return ctx.record("xp_a_" + table, rid)


def update(ctx, table, rid, **fields):
    return ctx.update("xp_a_" + table, rid, **fields)


def profile(ctx, number):
    return next((x for x in rows(ctx, "settlement_terms") if x["number"] == number), None)


@operation(
    "a_book_buyer_collection",
    "Purchase reserved buyer recollection capacity under a published service. The booking fee is nonrefundable. Booking holds capacity, does not collect money, and can be made before the return review determines the remaining debt.",
    service_id=("string", "Buyer recollection service ID."),
    capacity_minor=("integer", "Positive principal capacity in currency minor units."),
)
def book_collection(ctx, service_id, capacity_minor):
    s = row(ctx, "buyer_collection_services", service_id)
    p = profile(ctx, 32)
    ctx.require(p and s["sale_id"] == p["subject_id"], "COLLECTION_SCOPE")
    ctx.require(
        ctx.minute <= s["book_until"]
        and capacity_minor > 0
        and (capacity_minor <= s["max_capacity_minor"]),
        "BOOKING_WINDOW_OR_CAPACITY",
    )
    fee = (capacity_minor + s["fee_unit_minor"] - 1) // s["fee_unit_minor"] * s["booking_fee_minor"]
    ctx.ledger(p["account_id"], -fee, "USD", "BUYER_COLLECTION_BOOKING", service_id)
    return ctx.create(
        "xp_a_buyer_collection_bookings",
        service_id=service_id,
        sale_id=s["sale_id"],
        capacity_minor=capacity_minor,
        used_minor=0,
        fee_minor=fee,
        minute=ctx.minute,
    )


@operation(
    "a_collect_remaining_sale",
    "Request a chosen portion of the actual commercial debt through a previously booked buyer service. Concurrent requests count toward the same unpaid debt; bank receipts arrive after the service delay. Collection itself does not apply a receipt to the invoice.",
    sale_id=("string", "Original sale ID."),
    booking_id=("string", "Buyer collection booking."),
    amount_minor=("integer", "Positive principal to collect in minor units."),
)
def collect_sale(ctx, sale_id, booking_id, amount_minor):
    sale = row(ctx, "recall_sales", sale_id)
    p = profile(ctx, 32)
    b = row(ctx, "buyer_collection_bookings", booking_id)
    s = row(ctx, "buyer_collection_services", b["service_id"])
    inc = row(ctx, "incoming", sale["incoming_id"])
    ctx.require(p and p["subject_id"] == sale_id and (b["sale_id"] == sale_id), "COLLECTION_SCOPE")
    ctx.require(
        inc.get("decision_final") and inc["status"] in ("RETURNED", "CREDITED"),
        "ORIGINAL_PRINCIPAL_UNRESOLVED",
    )
    ordered = committed_buyer_principal(ctx, sale)
    retained = inc["amount_minor"] if inc["status"] == "CREDITED" else 0
    ctx.require(
        amount_minor > 0
        and amount_minor <= sale["face_minor"] - sale["credit_minor"] - retained - ordered,
        "COLLECTION_EXCEEDS_DEBT",
    )
    ctx.require(b["used_minor"] + amount_minor <= b["capacity_minor"], "BOOKED_CAPACITY_EXCEEDED")
    update(ctx, "buyer_collection_bookings", booking_id, used_minor=b["used_minor"] + amount_minor)
    order = ctx.create(
        "xp_a_buyer_collection_orders",
        sale_id=sale_id,
        booking_id=booking_id,
        amount_minor=amount_minor,
        status="PENDING",
        minute=ctx.minute,
        due_minute=ctx.minute + s["delay_minutes"],
    )
    ctx.schedule("a_reasoning_buyer_cash", order["due_minute"], order["id"])
    return order


@operation(
    "a_arrange_supplier_handover",
    "Purchase a supplier handover service for paid hardware. Actual delivery, followed by inspection, publishes the installation acceptance. Handover fees are charged at booking; a customer-uploaded delivery document alone does not establish this supplier event.",
    contract_id=("string", "Composite contract ID."),
    service_id=("string", "Supplier handover service ID."),
)
def arrange_handover(ctx, contract_id, service_id):
    p = profile(ctx, -28)
    s = row(ctx, "handover_services", service_id)
    ctx.require(
        p and p["subject_id"] == contract_id and (s["contract_id"] == contract_id), "HANDOVER_SCOPE"
    )
    ctx.require(not p.get("handover_booked"), "HANDOVER_ALREADY_BOOKED")
    goods = [
        x
        for x in rows(ctx, "components")
        if x["contract_id"] == contract_id and x["purpose"] == "GOODS"
    ]
    ctx.require(goods and all((x.get("last_payment_id") for x in goods)), "HARDWARE_NOT_PAID")
    ctx.ledger(p["account_id"], -s["fee_minor"], "USD", "SUPPLIER_HANDOVER_FEE", service_id)
    update(
        ctx,
        "settlement_terms",
        p["id"],
        handover_booked=True,
        handover_service_id=service_id,
        handover_due=ctx.minute + s["delay_minutes"],
    )
    ctx.schedule("a_reasoning_supplier_handover", ctx.minute + s["delay_minutes"], p["id"])
    return dict(
        status="BOOKED",
        delivery_due=ctx.minute + s["delay_minutes"],
        inspection_delay=p["inspection_delay_minutes"],
    )


@operation(
    "a_remit_collection_owner",
    "Remit a portion of a cleared agency collection to its recorded beneficial owner. The collection record and linked agency contract identify the destination. No extra debit is collected from the original debtor.",
    debt_id=("string", "Cleared agency collection debt ID."),
    amount_minor=("integer", "Positive minor units to remit."),
    recipient_id=("string", "Actual bank recipient."),
)
def remit_owner(ctx, debt_id, amount_minor, recipient_id):
    p = profile(ctx, -30)
    d = row(ctx, "debts", debt_id)
    recipient = row(ctx, "recipients", recipient_id)
    ctx.require(
        p and debt_id == p["agency_debt_id"] and (d.get("clearing_status") == "AVAILABLE"),
        "AGENCY_COLLECTION_NOT_AVAILABLE",
    )
    ctx.require(recipient["bank_enabled"] and amount_minor > 0, "REMITTANCE_ARGUMENTS")
    ctx.ledger(p["account_id"], -amount_minor, "USD", "AGENCY_OWNER_REMITTANCE", debt_id)
    return ctx.create(
        "xp_a_agency_remittances",
        debt_id=debt_id,
        recipient_id=recipient_id,
        amount_minor=amount_minor,
        minute=ctx.minute,
    )


def committed_buyer_principal(ctx, sale):
    received = sum(
        (
            r["amount_minor"] - r.get("refunded_minor", 0)
            for r in rows(ctx, "replacement_receipts")
            if r["sale_id"] == sale["id"]
        )
    )
    pending = sum(
        (
            o["amount_minor"]
            for o in rows(ctx, "buyer_collection_orders")
            if o["sale_id"] == sale["id"] and o["status"] == "PENDING"
        )
    )
    return (
        received
        + pending
        + (sale.get("requested_minor", 0) if sale.get("remittance_pending") else 0)
    )


def register(api):
    api.operations.update(OPS)
    old_recollect = api.operations["a_request_replacement_remittance"]["function"]

    def recollect(ctx, sale_id):
        p = profile(ctx, 32)
        if not p or sale_id != p["subject_id"]:
            return old_recollect(ctx, sale_id)
        sale = row(ctx, "recall_sales", sale_id)
        inc = row(ctx, "incoming", sale["incoming_id"])
        ctx.require(
            inc.get("decision_final") and inc["status"] in ("RETURNED", "CREDITED"),
            "ORIGINAL_PRINCIPAL_UNRESOLVED",
        )
        ctx.require(not sale.get("remittance_pending"), "BUYER_REMITTANCE_PENDING")
        retained = inc["amount_minor"] if inc["status"] == "CREDITED" else 0
        amount = (
            sale["face_minor"]
            - sale["credit_minor"]
            - retained
            - committed_buyer_principal(ctx, sale)
        )
        ctx.require(amount > 0, "NO_REMAINING_BUYER_BALANCE")
        ctx.schedule(
            "a_special_buyer_remittance",
            ctx.minute + sale["buyer_delay_minutes"],
            sale_id,
            amount_minor=amount,
        )
        return update(ctx, "recall_sales", sale_id, remittance_pending=True, requested_minor=amount)

    api.operations["a_request_replacement_remittance"]["function"] = recollect
    old_decl = api.operations["a_declare_contract_payment"]["function"]

    def declare(ctx, **kw):
        result = old_decl(ctx, **kw)
        p = profile(ctx, -28)
        if not p or kw["contract_id"] != p["subject_id"]:
            return result
        accepted = []
        for cid in result["accepted_component_ids"]:
            c = row(ctx, "components", cid)
            if c["purpose"] == "LICENSE" and (not p.get("handover_complete")):
                continue
            if c["purpose"] == "SERVICE" and (not p.get("inspection_complete")):
                continue
            accepted.append(cid)
        return update(ctx, "declarations", result["id"], accepted_component_ids=accepted)

    api.operations["a_declare_contract_payment"]["function"] = declare
    old_execute = api.operations["a_execute_contract_declaration"]["function"]

    def execute(ctx, **kw):
        d = row(ctx, "declarations", kw["declaration_id"])
        p = profile(ctx, -28)
        if p and d["contract_id"] == p["subject_id"]:
            for cid in d["accepted_component_ids"]:
                c = row(ctx, "components", cid)
                if c["purpose"] == "SERVICE":
                    ctx.require(
                        p.get("inspection_complete")
                        and d["route_id"] in p["installation_route_ids"],
                        "INSTALLATION_ROUTE_ACCEPTANCE",
                    )
        return old_execute(ctx, **kw)

    api.operations["a_execute_contract_declaration"]["function"] = execute
    old_cash = api.operations["a_reserve_cash_pickup"]["function"]

    def reserve(ctx, **kw):
        a = row(ctx, "cash_agents", kw["agent_id"])
        p = profile(ctx, -31)
        if p and a["id"] in p["agent_ids"]:
            ctx.require(ctx.minute <= a["book_until"], "CASH_AGENT_BOOKING_CLOSED")
        return old_cash(ctx, **kw)

    api.operations["a_reserve_cash_pickup"]["function"] = reserve
    old_deliver = api.operations["a_deliver_reserved_cash"]["function"]

    def deliver(ctx, **kw):
        b = row(ctx, "cash_reservations", kw["reservation_id"])
        a = row(ctx, "cash_agents", b["agent_id"])
        p = profile(ctx, -31)
        if p and a["id"] in p["agent_ids"]:
            ctx.require(ctx.minute >= a["handover_from"], "CASH_AGENT_NOT_AT_HANDOVER_DESK")
        return old_deliver(ctx, **kw)

    api.operations["a_deliver_reserved_cash"]["function"] = deliver
    old_event = api.event_handler

    def event(ctx, e):
        kind = e["kind"]
        if kind == "a_reasoning_buyer_cash":
            o = row(ctx, "buyer_collection_orders", e["subject_id"])
            sale = row(ctx, "recall_sales", o["sale_id"])
            ctx.ledger(
                sale["account_id"], o["amount_minor"], "USD", "BUYER_REPLACEMENT_CASH", sale["id"]
            )
            ctx.create(
                "xp_a_replacement_receipts",
                sale_id=sale["id"],
                amount_minor=o["amount_minor"],
                minute=ctx.minute,
                applied=False,
                collection_order_id=o["id"],
            )
            update(
                ctx,
                "recall_sales",
                sale["id"],
                replacement_received_minor=sale.get("replacement_received_minor", 0)
                + o["amount_minor"],
            )
            update(
                ctx,
                "buyer_collection_orders",
                o["id"],
                status="RECEIVED",
                received_minute=ctx.minute,
            )
            return True
        if kind == "a_reasoning_supplier_handover":
            p = row(ctx, "settlement_terms", e["subject_id"])
            update(
                ctx, "settlement_terms", p["id"], handover_complete=True, handover_minute=ctx.minute
            )
            ctx.schedule(
                "a_reasoning_installation_inspection",
                ctx.minute + p["inspection_delay_minutes"],
                p["id"],
            )
            return True
        if kind == "a_reasoning_installation_inspection":
            p = row(ctx, "settlement_terms", e["subject_id"])
            update(
                ctx,
                "settlement_terms",
                p["id"],
                inspection_complete=True,
                inspection_minute=ctx.minute,
                installation_route_ids=p["_installation_route_ids"],
            )
            return True
        return old_event(ctx, e)

    api.event_handler = event
