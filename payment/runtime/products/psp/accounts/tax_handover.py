"""Finite agency processing commitments beside retiring authority receipts."""

from copy import deepcopy

TABLE = "xp_b_reasoning_tax_capacity"
BOOK = "xp_b_reasoning_tax_bookings"
PAY = "xp_b_reasoning_tax_payments"
OPS = {}
S = {"type": "string"}
I = {"type": "integer", "minimum": 1}


def context(ctx, obligation_id=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (obligation_id is None or obligation_id in r["obligation_ids"])
        ),
        None,
    )


def op(name, description, params):

    def f(fn):
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=params, required=list(params), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return f


@op(
    "reserve_tax_agency_processing",
    "Buy agency processing capacity for one jurisdiction before the public booking cutoff. Pending agent approval does not prevent a reservation. Capacity is denominated in tax principal, shares the contract limit across jurisdictions, and charges a nonrefundable reservation fee. It neither pays tax nor creates a tax credit.",
    {"capacity_contract_id": S, "jurisdiction": S, "amount_minor": I},
)
def reserve(ctx, capacity_contract_id, jurisdiction, amount_minor):
    r = ctx.record(TABLE, capacity_contract_id)
    ctx.require(
        context(ctx) is not None
        and jurisdiction in r["jurisdictions"]
        and (ctx.minute <= r["booking_cutoff"]),
        "AGENCY_RESERVATION_SCOPE_OR_CUTOFF",
    )
    total = sum((b["reserved_minor"] for b in ctx.records(BOOK, capacity_contract_id=r["id"])))
    ctx.require(total + amount_minor <= r["total_capacity_minor"], "AGENCY_SHARED_CAPACITY")
    ctx.ledger(r["account_id"], -r["booking_fee_minor"], "USD", "tax_capacity_reservation", r["id"])
    return ctx.create(
        BOOK,
        customer_id=r["customer_id"],
        capacity_contract_id=r["id"],
        jurisdiction=jurisdiction,
        reserved_minor=amount_minor,
        used_minor=0,
        minute=ctx.minute,
    )


@op(
    "transfer_unused_tax_agency_capacity",
    "Move unused reserved processing principal between the contract jurisdictions before its transfer cutoff. Used capacity cannot be transferred. The amendment charge is real cash and the total agency limit is unchanged.",
    {"booking_id": S, "jurisdiction": S, "amount_minor": I},
)
def transfer(ctx, booking_id, jurisdiction, amount_minor):
    b = ctx.record(BOOK, booking_id)
    r = ctx.record(TABLE, b["capacity_contract_id"])
    ctx.require(
        jurisdiction in r["jurisdictions"]
        and ctx.minute <= r["transfer_cutoff"]
        and (amount_minor <= b["reserved_minor"] - b["used_minor"]),
        "CAPACITY_TRANSFER_WINDOW",
    )
    ctx.ledger(r["account_id"], -r["transfer_fee_minor"], "USD", "tax_capacity_amendment", r["id"])
    ctx.update(BOOK, b["id"], reserved_minor=b["reserved_minor"] - amount_minor)
    return ctx.create(
        BOOK,
        customer_id=r["customer_id"],
        capacity_contract_id=r["id"],
        jurisdiction=jurisdiction,
        reserved_minor=amount_minor,
        used_minor=0,
        minute=ctx.minute,
        transferred_from=b["id"],
    )


def register(operations, old_event):
    operations.update(OPS)
    original = operations["pay_tax_obligation"]["function"]
    application = operations["submit_tax_agent_application"]["function"]

    def apply(ctx, agent_id):
        r = context(ctx)
        if r and agent_id == r["agent_id"]:
            ctx.require(
                any(
                    (
                        set(s.get("content", {}).get("jurisdictions", []))
                        == set(r["jurisdictions"])
                        for s in ctx.uploaded(agent_id, "tax_agent_appointment")
                    )
                ),
                "APPOINTMENT_JURISDICTION_SCOPE",
            )
        return application(ctx, agent_id=agent_id)

    operations["submit_tax_agent_application"]["function"] = apply

    def payment(ctx, obligation_id, recipient_id):
        r = context(ctx, obligation_id)
        if not r:
            return original(ctx, obligation_id=obligation_id, recipient_id=recipient_id)
        o = ctx.record("xp_b_tax_obligations", obligation_id)
        agent = ctx.record("xp_b_tax_agents", recipient_id)
        amount = o["amount_minor"]
        bookings = []
        if agent["kind"] == "agent":
            bookings = [
                x
                for x in ctx.records(BOOK, capacity_contract_id=r["id"])
                if x["jurisdiction"] == o["jurisdiction"]
            ]
            ctx.require(
                sum((x["reserved_minor"] - x["used_minor"] for x in bookings)) >= amount,
                "JURISDICTION_PROCESSING_CAPACITY",
            )
            delay = r["agency_delay"]
        else:
            route = r["direct_routes"][recipient_id]
            ctx.require(amount <= route["maximum_minor"], "DIRECT_AUTHORITY_AMOUNT_LIMIT")
            delay = route["delay"]
        result = original(ctx, obligation_id=obligation_id, recipient_id=recipient_id)
        remaining = amount
        for x in bookings:
            used = min(remaining, x["reserved_minor"] - x["used_minor"])
            ctx.update(BOOK, x["id"], used_minor=x["used_minor"] + used)
            remaining -= used
        p = ctx.create(
            PAY,
            customer_id=r["customer_id"],
            obligation_id=o["id"],
            recipient_id=recipient_id,
            amount_minor=amount,
            submitted_minute=ctx.minute,
            received_minor=0,
            status="in_transit",
            valid_jurisdiction=result["discharged"],
            capacity_snapshot=[deepcopy(x) for x in bookings],
        )
        ctx.schedule("b_reasoning_tax_receipt", ctx.minute + delay, p["id"])
        return result

    operations["pay_tax_obligation"]["function"] = payment

    def event(ctx, e):
        r = context(ctx)
        if e["kind"] == "b_reasoning_tax_receipt":
            p = ctx.record(PAY, e["subject_id"])
            ctx.update(
                PAY,
                p["id"],
                status="received",
                received_minor=p["amount_minor"] if p["valid_jurisdiction"] else 0,
                received_minute=ctx.minute,
            )
            return True
        if r and e["kind"] in ("b_revision_tax_census", "b_revision_tax_settle"):
            for x in ctx.records("xp_b_retiring_tax_instructions"):
                if x["id"] in r["_final_allocations"]:
                    ctx.update(
                        "xp_b_retiring_tax_instructions",
                        x["id"],
                        authority_allocations=deepcopy(r["_final_allocations"][x["id"]]),
                        allocation_basis="final",
                    )
        return old_event(ctx, e)

    return event
