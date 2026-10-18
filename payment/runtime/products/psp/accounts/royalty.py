"""Opt-in royalty delivery capacity and effective beneficiary conditions."""

from copy import deepcopy

VERSION = "b-reasoning-1"
TABLE = "xp_b_reasoning_royalty_delivery"
CHANNELS = "xp_b_reasoning_royalty_channels"
BOOKINGS = "xp_b_reasoning_royalty_bookings"
S = {"type": "string"}
OPS = {}


def context(ctx, pool_id=None):
    if not any(
        (
            c.get("reasoning_version") == VERSION and c["customer_id"] == ctx.customer_id
            for c in ctx.records("xp_cases")
        )
    ):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r.get("reasoning_version") == VERSION
            and (pool_id is None or r["pool_id"] == pool_id)
            and (r["customer_id"] == ctx.customer_id)
        ),
        None,
    )


def op(name, description, params):

    def register(fn):
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=params, required=list(params), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return register


@op(
    "reserve_royalty_payout_capacity",
    "Reserve a delivery route for one beneficiary allocation. The public service fee is immediately debited. channel_id specifies the service and shared capacity_group; all services in that group consume the same finite instruction slots. Batch channels additionally cap the total net amount submitted. Reservation is not payment or confirmation of final beneficiary eligibility.",
    {"allocation_id": S, "channel_id": S},
)
def reserve(ctx, allocation_id, channel_id):
    a = ctx.record("xp_b_allocations", allocation_id)
    r = context(ctx, a["pool_id"])
    ctx.require(r is not None, "ROYALTY_DELIVERY_PRODUCT_SCOPE")
    ch = ctx.record(CHANNELS, channel_id)
    ctx.require(ch["delivery_contract_id"] == r["id"], "DELIVERY_CONTRACT_MISMATCH")
    existing = ctx.records(BOOKINGS, delivery_contract_id=r["id"])
    group = [
        b
        for b in existing
        if b["capacity_group"] == ch["capacity_group"] and b["status"] != "cancelled"
    ]
    ctx.require(
        len(group) < r["capacity_limits"][ch["capacity_group"]], "PAYOUT_INSTRUCTION_CAPACITY"
    )
    ctx.require(ctx.minute <= ch["cutoff_minute"], "PAYOUT_CHANNEL_CUTOFF")
    ctx.ledger(r["account_id"], -ch["fee_minor"], "USD", "royalty_delivery_booking_fee", r["id"])
    return ctx.create(
        BOOKINGS,
        customer_id=r["customer_id"],
        delivery_contract_id=r["id"],
        allocation_id=a["id"],
        channel_id=ch["id"],
        capacity_group=ch["capacity_group"],
        status="reserved",
        reserved_minute=ctx.minute,
        fee_minor=ch["fee_minor"],
    )


@op(
    "cancel_unused_royalty_capacity",
    "Release one unused payout reservation. Its service fee is not refunded. Submitted instructions still consume the shared execution slot and cannot be erased by cancellation.",
    {"booking_id": S},
)
def cancel(ctx, booking_id):
    b = ctx.record(BOOKINGS, booking_id)
    ctx.require(b["status"] == "reserved", "PAYOUT_ALREADY_SUBMITTED")
    return ctx.update(BOOKINGS, b["id"], status="cancelled", cancelled_minute=ctx.minute)


@op(
    "select_royalty_payout_booking",
    "Select an unsubmitted capacity booking for the next disburse_allocation instruction of its beneficiary. Selection does not change the beneficiary, monetary amount, channel rules or delivery deadline.",
    {"booking_id": S},
)
def select(ctx, booking_id):
    b = ctx.record(BOOKINGS, booking_id)
    ctx.require(b["status"] == "reserved", "PAYOUT_BOOKING_STATE")
    a = ctx.record("xp_b_allocations", b["allocation_id"])
    return ctx.update("xp_b_allocations", a["id"], delivery_booking_id=b["id"])


def register(operations, old_event):
    operations.update(OPS)
    original = operations["disburse_allocation"]["function"]
    review = operations["review_royalty_beneficiary_residency"]["function"]

    def routed(ctx, allocation_id, amount_minor):
        a = ctx.record("xp_b_allocations", allocation_id)
        r = context(ctx, a["pool_id"])
        if r is None:
            return original(ctx, allocation_id=allocation_id, amount_minor=amount_minor)
        b = ctx.record(BOOKINGS, a.get("delivery_booking_id", ""))
        ch = ctx.record(CHANNELS, b["channel_id"])
        ctx.require(
            b["status"] == "reserved"
            and b["allocation_id"] == allocation_id
            and (ctx.minute <= ch["cutoff_minute"]),
            "PAYOUT_BOOKING_BINDING",
        )
        submitted = sum(
            (
                x.get("amount_minor", 0)
                for x in ctx.records(BOOKINGS, channel_id=ch["id"])
                if x["status"] in ("submitted", "delivered")
            )
        )
        ctx.require(
            submitted + amount_minor <= ch["amount_capacity_minor"], "PAYOUT_BATCH_AMOUNT_CAPACITY"
        )
        result = original(ctx, allocation_id=allocation_id, amount_minor=amount_minor)
        due = ch["arrival_minute"] if "arrival_minute" in ch else ctx.minute + ch["delay_minutes"]
        ctx.update(
            BOOKINGS,
            b["id"],
            status="submitted",
            amount_minor=amount_minor,
            submitted_minute=ctx.minute,
            conditions_final_at_submission=r.get("conditions_final", False),
            allowed_channels_at_submission=deepcopy(
                r["beneficiary_conditions"][allocation_id]["allowed_channels"]
            ),
        )
        ctx.schedule("b_reasoning_royalty_delivery", max(ctx.minute, due), b["id"])
        return result

    operations["disburse_allocation"]["function"] = routed

    def reviewed(ctx, record_id, **kwargs):
        old = ctx.record("xp_b_royalty_tax", record_id)
        r = context(ctx, old["pool_id"])
        result = review(ctx, record_id=record_id, **kwargs)
        if r:
            for e in ctx.records("xp_events"):
                if (
                    e["subject_id"] == record_id
                    and e["kind"] == "b_enhance_royalty_residency"
                    and (e["status"] == "SCHEDULED")
                ):
                    ctx.update(
                        "xp_events", e["id"], due_minute=ctx.minute + r["review_delay_minutes"]
                    )
        return result

    operations["review_royalty_beneficiary_residency"]["function"] = reviewed

    def handle(ctx, e):
        if e["kind"] == "b_reasoning_royalty_delivery":
            b = ctx.record(BOOKINGS, e["subject_id"])
            r = ctx.record(TABLE, b["delivery_contract_id"])
            ch = ctx.record(CHANNELS, b["channel_id"])
            allowed = b["channel_id"] in b["allowed_channels_at_submission"] and (
                not ch["requires_final_conditions"] or b["conditions_final_at_submission"]
            )
            ctx.update(
                BOOKINGS,
                b["id"],
                status="delivered" if allowed else "return_pending",
                received_minor=b["amount_minor"] if allowed else 0,
                receipt_minute=ctx.minute,
                result="received" if allowed else "beneficiary_conditions_not_satisfied",
                in_transit_minor=0 if allowed else b["amount_minor"],
            )
            if not allowed:
                ctx.schedule(
                    "b_reasoning_royalty_return",
                    ctx.minute + r["rejected_return_delay_minutes"],
                    b["id"],
                )
            return True
        if e["kind"] == "b_reasoning_royalty_return":
            b = ctx.record(BOOKINGS, e["subject_id"])
            a = ctx.record("xp_b_allocations", b["allocation_id"])
            p = ctx.record("xp_b_pools", a["pool_id"])
            ctx.update(
                "xp_b_pools", p["id"], remaining_minor=p["remaining_minor"] + b["in_transit_minor"]
            )
            ctx.update(
                "xp_b_allocations", a["id"], paid_minor=a["paid_minor"] - b["in_transit_minor"]
            )
            ctx.update(
                BOOKINGS,
                b["id"],
                status="returned",
                returned_minor=b["in_transit_minor"],
                in_transit_minor=0,
                returned_minute=ctx.minute,
            )
            return True
        result = old_event(ctx, e)
        if e["kind"] == "b_enhance_royalty_residency":
            tax = ctx.record("xp_b_royalty_tax", e["subject_id"])
            r = context(ctx, tax["pool_id"])
            if r:
                ctx.update(
                    TABLE,
                    r["id"],
                    conditions_final=True,
                    beneficiary_conditions=deepcopy(r["_final_beneficiary_conditions"]),
                    conditions_minute=ctx.minute,
                )
        return result

    return handle
