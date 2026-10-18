"""Accepted advances and recovered executor limits; customer/object-scoped opt-in."""

from copy import deepcopy

VERSION = "b-reasoning-1"
TABLE = "xp_b_reasoning_recovery"
ADV = "xp_b_reasoning_accepted_advances"
OPS = {}
S = {"type": "string"}


def context(ctx, obligation_id=None):
    if not any((x.get("reasoning_version") == VERSION for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == VERSION
            and (obligation_id is None or r["obligation_id"] == obligation_id)
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
    "release_accepted_supplier_advance",
    "Release one of the alternative pre-accepted supplier advances. This bank-accepted instruction predates the lost credential and remains executable during recovery. It reserves its nominal cash until the scheduled settlement and charges its own fee. The supplier may credit only the public bounded range; excess reserved principal is released. Both options belong to the same original invoice, so only one alternative is released.",
    {"advance_id": S},
)
def release(ctx, advance_id):
    a = ctx.record(ADV, advance_id)
    r = context(ctx, a["obligation_id"])
    ctx.require(
        r is not None and a["status"] == "held" and (not r.get("released_advance_id")),
        "ADVANCE_ALTERNATIVE_ALREADY_COMMITTED",
    )
    ctx.ledger(
        r["account_id"],
        -(a["nominal_minor"] + a["fee_minor"]),
        "USD",
        "accepted_supplier_advance_reserve",
        a["id"],
    )
    ctx.update(
        ADV,
        a["id"],
        status="released",
        released_minute=ctx.minute,
        reserved_minor=a["nominal_minor"],
    )
    ctx.update(TABLE, r["id"], released_advance_id=a["id"])
    ctx.schedule("b_reasoning_advance_settlement", ctx.minute + a["delay_minutes"], a["id"])
    return {"advance_id": a["id"], "status": "released", "nominal_minor": a["nominal_minor"]}


@op(
    "apply_supplier_advance_receipt",
    "Apply one actual settled advance to the original supplier invoice. Only received principal reduces the remaining invoice; a reserved amount or service charge is not supplier payment. This does not rewrite the amount in an earlier held instruction or authentication challenge.",
    {"advance_id": S},
)
def apply(ctx, advance_id):
    a = ctx.record(ADV, advance_id)
    r = context(ctx, a["obligation_id"])
    o = ctx.record("xp_b_secure_obligations", a["obligation_id"])
    ctx.require(
        r is not None and a["status"] == "settled" and (not a.get("applied")) and (not o["paid"]),
        "ADVANCE_RECEIPT_STATE",
    )
    ctx.require(a["received_minor"] <= o["amount_minor"], "ADVANCE_EXCEEDS_INVOICE")
    ctx.update(
        "xp_b_secure_obligations", o["id"], amount_minor=o["amount_minor"] - a["received_minor"]
    )
    return ctx.update(ADV, a["id"], applied=True, applied_minute=ctx.minute)


@op(
    "amend_held_supplier_balance",
    "Revise a held instruction to the original invoice’s presently outstanding amount, after applying actual supplier advance receipts. This preserves the obligation and old credential epoch; migrate or replace still establishes a current execution instruction. Existing challenges keep their original amounts.",
    {"instruction_id": S},
)
def amend(ctx, instruction_id):
    x = ctx.record("xp_b_recovery_instructions", instruction_id)
    r = context(ctx, x["obligation_id"])
    o = ctx.record("xp_b_secure_obligations", x["obligation_id"])
    ctx.require(
        r is not None
        and x["status"] == "held"
        and any((a.get("applied") for a in ctx.records(ADV) if a["obligation_id"] == o["id"])),
        "ACTUAL_ADVANCE_ALLOCATION_REQUIRED",
    )
    return ctx.update(
        "xp_b_recovery_instructions",
        x["id"],
        amount_minor=o["amount_minor"],
        prior_amount_minor=x["amount_minor"],
        amended_minute=ctx.minute,
    )


def register(operations, old_event):
    operations.update(OPS)

    def event(ctx, e):
        if e["kind"] == "b_reasoning_advance_settlement":
            a = ctx.record(ADV, e["subject_id"])
            r = context(ctx, a["obligation_id"])
            amount = a["_settlement_minor"]
            unused = a["nominal_minor"] - amount
            if unused:
                ctx.ledger(
                    r["account_id"], unused, "USD", "supplier_advance_reservation_release", a["id"]
                )
            ctx.update(
                ADV,
                a["id"],
                status="settled",
                received_minor=amount,
                received_minute=ctx.minute,
                reserved_minor=0,
                released_unused_minor=unused,
            )
            return True
        return old_event(ctx, e)

    return event
