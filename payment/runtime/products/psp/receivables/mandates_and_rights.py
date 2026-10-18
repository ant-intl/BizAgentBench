"""Per-claim collection mandates and shared successor execution capacity."""

from copy import deepcopy
from payment.runtime.products.psp.receivables.dispatch import (
    rows,
    row,
    update,
    profile,
    need,
    ledger,
)

OPS = {}
S = "string"
I = "integer"


def op(name, description, **fields):

    def dec(fn):
        props = {k: dict(type=t, description=d) for (k, (t, d)) in fields.items()}
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=props, required=list(props), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return dec


@op(
    "a_revise_receivable_collection_mandate",
    "Replace the portfolio collection mandate or add a separate mandate for one claim. Fees are nonrefundable; replacing revokes previous unspent mandates. A directory payee may be bank-valid without owning this claim.",
    contract_id=(S, "Original receivable portfolio contract."),
    subject_id=(S, "One original receivable or retained claim ID."),
    payee_id=(S, "Bank directory legal payee ID."),
    mode=(S, "REPLACE or ADD."),
)
def revise_collection(ctx, contract_id, subject_id, payee_id, mode):
    p = need(ctx, contract_id, -24)
    ctx.require(
        subject_id in p["subject_ids"]
        and payee_id in p["payee_ids"]
        and (mode in p["mandate_fees_minor"]),
        "COLLECTION_MANDATE_SCOPE",
    )
    ledger(ctx, p, -p["mandate_fees_minor"][mode], "COLLECTION_MANDATE_FEE", subject_id)
    if mode == "REPLACE":
        for m in rows(ctx, "collection_mandates"):
            if m["contract_id"] == p["id"] and m["status"] == "ACTIVE":
                update(ctx, "collection_mandates", m["id"], status="REVOKED")
    return ctx.create(
        "xp_a_collection_mandates",
        customer_id=p["customer_id"],
        contract_id=p["id"],
        subject_ids=[subject_id],
        payee_id=payee_id,
        status="ACTIVE",
        created_minute=ctx.minute,
    )


def collection(ctx, p, subject):
    m = next(
        (
            m
            for m in reversed(rows(ctx, "collection_mandates"))
            if m["contract_id"] == p["id"]
            and subject in m["subject_ids"]
            and (m["status"] == "ACTIVE")
        ),
        None,
    )
    ctx.require(m is not None, "CLAIM_COLLECTION_MANDATE_MISSING")
    return m


@op(
    "a_pay_retained_receivable_claim",
    "Pay a separate retained original receivable through its active collection mandate. Payout transfers real principal to the bank-valid mandate payee and consumes this claim once; portfolio succession does not automatically transfer every claim.",
    claim_id=(S, "Original retained receivable ID."),
)
def retained(ctx, claim_id):
    claim = row(ctx, "retained_receivables", claim_id)
    p = need(ctx, claim["contract_id"], -24)
    ctx.require(not claim.get("paid"), "CLAIM_ALREADY_PAID")
    m = collection(ctx, p, claim_id)
    ledger(ctx, p, -claim["amount_minor"], "RETAINED_RECEIVABLE_PAYOUT", claim_id)
    return update(
        ctx,
        "retained_receivables",
        claim_id,
        paid=True,
        paid_payee_id=m["payee_id"],
        paid_minute=ctx.minute,
        mandate_id=m["id"],
    )


@op(
    "a_allocate_executor_capacity",
    "Reserve approved successor capacity for one original obligation. Reservations and consumed amounts share each executor limit. The first reservation on a charged desk incurs its published access fee; releasing unused capacity does not refund access.",
    contract_id=(S, "Original finance handover portfolio."),
    executor_id=(S, "Approved execution desk."),
    subject_id=(S, "Original invoice or payroll obligation."),
    amount_minor=(I, "Amount of capacity in USD minor units."),
)
def allocate(ctx, contract_id, executor_id, subject_id, amount_minor):
    p = need(ctx, contract_id, -25)
    desk = row(ctx, "execution_desks", executor_id)
    ctx.require(
        p.get("limits_final")
        and desk["contract_id"] == p["id"]
        and (subject_id in desk["subject_ids"])
        and (amount_minor > 0),
        "EXECUTOR_CAPACITY_SCOPE",
    )
    ctx.require(desk["remaining_minor"] >= amount_minor, "EXECUTOR_CAPACITY_EXHAUSTED")
    if not desk.get("access_paid"):
        ledger(ctx, p, -desk["access_fee_minor"], "EXECUTOR_DESK_FEE", desk["id"])
    update(
        ctx,
        "execution_desks",
        desk["id"],
        access_paid=True,
        remaining_minor=desk["remaining_minor"] - amount_minor,
    )
    return ctx.create(
        "xp_a_executor_allocations",
        customer_id=p["customer_id"],
        contract_id=p["id"],
        executor_id=executor_id,
        subject_id=subject_id,
        amount_minor=amount_minor,
        status="RESERVED",
    )


@op(
    "a_release_executor_capacity",
    "Release an unconsumed successor allocation. Paid principal and desk access fees are not reversed.",
    allocation_id=(S, "Unused executor allocation ID."),
)
def release(ctx, allocation_id):
    a = row(ctx, "executor_allocations", allocation_id)
    ctx.require(a["status"] == "RESERVED", "EXECUTOR_ALLOCATION_CONSUMED")
    desk = row(ctx, "execution_desks", a["executor_id"])
    update(
        ctx,
        "execution_desks",
        desk["id"],
        remaining_minor=desk["remaining_minor"] + a["amount_minor"],
    )
    return update(ctx, "executor_allocations", a["id"], status="RELEASED")


@op(
    "a_execute_handover_payroll",
    "Execute the original payroll obligation through a reserved successor allocation. Actual operating cash is debited and the employee receives it after the published payroll clearing interval. The used desk limit is consumed for this period.",
    contract_id=(S, "Finance handover portfolio."),
    allocation_id=(S, "Reserved payroll capacity ID."),
)
def payroll(ctx, contract_id, allocation_id):
    p = need(ctx, contract_id, -25)
    a = row(ctx, "executor_allocations", allocation_id)
    job = row(ctx, "handover_payroll", p["payroll_id"])
    ctx.require(
        a["contract_id"] == p["id"]
        and a["subject_id"] == job["id"]
        and (a["status"] == "RESERVED")
        and (a["amount_minor"] == job["amount_minor"])
        and (not job.get("submitted")),
        "PAYROLL_ALLOCATION_STATE",
    )
    ledger(ctx, p, -job["amount_minor"], "HANDOVER_PAYROLL", job["id"])
    update(ctx, "executor_allocations", a["id"], status="CONSUMED")
    update(
        ctx,
        "handover_payroll",
        job["id"],
        submitted=True,
        submitted_minute=ctx.minute,
        executor_id=a["executor_id"],
        allocation_id=a["id"],
    )
    ctx.schedule("a_reasoning_payroll_available", ctx.minute + job["clearing_minutes"], job["id"])
    return {"status": "PROCESSING"}


def register(api):
    api.operations.update(OPS)
    old_event = api.event_handler
    old_payout = api.operations["a_pay_out_shop_receipt"]["function"]
    old_transfer = api.operations["a_execute_transferred_payment"]["function"]

    def payout(ctx, **args):
        p = profile(ctx, -24, args["receipt_id"])
        if not p:
            return old_payout(ctx, **args)
        m = collection(ctx, p, args["receipt_id"])
        deed = next(
            (x for x in rows(ctx, "receivable_successions") if x["source_id"] == args["receipt_id"])
        )
        ctx.require(
            m["payee_id"] == deed.get("bound_payee_id"), "COLLECTION_PAYEE_BINDING_MISMATCH"
        )
        result = old_payout(ctx, **args)
        update(
            ctx,
            "virtual_receipts",
            args["receipt_id"],
            collection_mandate_id=m["id"],
            paid_minute=ctx.minute,
        )
        return result

    def transferred(ctx, **args):
        intent = row(ctx, "payment_intents", args["intent_id"])
        p = profile(ctx, -25, intent["invoice_id"])
        if not p:
            return old_transfer(ctx, **args)
        inv = row(ctx, "invoices", intent["invoice_id"])
        dedup = args["payment_key"] == intent["original_key"] and intent["bank_status"] in (
            "UNKNOWN",
            "ACCEPTED",
            "SETTLED",
        )
        a = next(
            (
                a
                for a in rows(ctx, "executor_allocations")
                if a["subject_id"] == inv["id"]
                and a["executor_id"] == args["executor_id"]
                and (a["status"] == "RESERVED")
                and (a["amount_minor"] >= inv["amount_minor"])
            ),
            None,
        )
        if not dedup:
            ctx.require(a is not None, "TRANSFERRED_INVOICE_CAPACITY_REQUIRED")
        result = old_transfer(ctx, **args)
        if not dedup:
            update(ctx, "executor_allocations", a["id"], status="CONSUMED")
        return result

    def event(ctx, e):
        if e["kind"] == "a_reasoning_payroll_available":
            update(ctx, "handover_payroll", e["subject_id"], paid=True, available_minute=ctx.minute)
            return True
        result = old_event(ctx, e)
        if e["kind"] == "a_diversity_handover":
            p = profile(ctx, -25)
            if p and p["handover_id"] == e["subject_id"]:
                for desk in rows(ctx, "execution_desks"):
                    if desk["contract_id"] == p["id"]:
                        update(
                            ctx,
                            "execution_desks",
                            desk["id"],
                            approved_limit_minor=desk["_approved_limit_minor"],
                            remaining_minor=desk["_approved_limit_minor"],
                        )
                update(ctx, "reasoning_contracts", p["id"], limits_final=True)
        return result

    api.operations["a_pay_out_shop_receipt"]["function"] = payout
    api.operations["a_execute_transferred_payment"]["function"] = transferred
    api.event_handler = event
