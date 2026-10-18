"""Opt-in custody service costs and supplier credit-note allocation."""

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


def op(name, description, **fields):

    def wrap(fn):
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

    return wrap


S = "string"


@op(
    "a_order_trustee_review",
    "Buy a trustee onboarding review. The nonrefundable fee is charged to operating cash; the completed review publishes actual activation offers for the named dispute trustee.",
    contract_id=(S, "Dispute custody service contract."),
    service_id=(S, "Published review service ID."),
)
def review(ctx, contract_id, service_id):
    p = need(ctx, contract_id, 31)
    s = row(ctx, "custody_services", service_id)
    ctx.require(
        s["contract_id"] == p["id"] and s["stage"] == "REVIEW" and (not p.get("review_order_id")),
        "CUSTODY_REVIEW_STATE",
    )
    from payment.runtime.products.psp.receivables.ownership_and_handover import BASE

    ctx.require(
        BASE["_evidence"](ctx, p["subject_ids"][0], "escrow_direction"),
        "MATCHING_ESCROW_DIRECTION_REQUIRED",
    )
    ledger(ctx, p, -s["fee_minor"], "CUSTODY_REVIEW_FEE", s["id"])
    update(
        ctx,
        "reasoning_contracts",
        p["id"],
        review_order_id=s["id"],
        review_ordered_minute=ctx.minute,
    )
    ctx.schedule("a_reasoning_custody_review", ctx.minute + s["delay_minutes"], p["id"])
    return {"status": "UNDER_REVIEW", "contract_id": p["id"]}


@op(
    "a_activate_dispute_custody",
    "Purchase the selected trustee activation after review. Operating cash pays the fee, and custody becomes usable only on the actual activation event. It does not transfer segregated principal.",
    contract_id=(S, "Reviewed dispute custody contract."),
    service_id=(S, "Published activation service ID."),
)
def activate(ctx, contract_id, service_id):
    p = need(ctx, contract_id, 31)
    s = row(ctx, "custody_services", service_id)
    ctx.require(
        p.get("review_complete")
        and (not p.get("activation_order_id"))
        and (s["contract_id"] == p["id"])
        and (s["stage"] == "ACTIVATION"),
        "CUSTODY_ACTIVATION_STATE",
    )
    ledger(ctx, p, -s["fee_minor"], "CUSTODY_ACTIVATION_FEE", s["id"])
    update(
        ctx,
        "reasoning_contracts",
        p["id"],
        activation_order_id=s["id"],
        activation_ordered_minute=ctx.minute,
    )
    ctx.schedule("a_reasoning_custody_active", ctx.minute + s["delay_minutes"], p["id"])
    return {"status": "ACTIVATING", "due_minute": ctx.minute + s["delay_minutes"]}


@op(
    "a_request_supplier_credit_confirmation",
    "Request the supplier confirmation of the listed original-purchase credit notes. The response publishes current refund fees and value dates; it neither reduces an invoice nor creates cash.",
    contract_id=(S, "Supplier credit-note agreement."),
)
def credit_review(ctx, contract_id):
    p = need(ctx, contract_id, 33)
    ctx.require(not p.get("credit_review_requested"), "CREDIT_REVIEW_EXISTS")
    update(ctx, "reasoning_contracts", p["id"], credit_review_requested=True)
    ctx.schedule(
        "a_reasoning_credit_confirmation",
        max(ctx.minute + 1, p["credit_confirmation_minute"]),
        p["id"],
    )
    return {"status": "PENDING"}


@op(
    "a_apply_supplier_credit_note",
    "Apply one confirmed unused credit note to a reopened original supplier invoice. The merchant permits the published number of instruments per invoice; additional stacked instruments are consumed without discharging further principal under nonstacking terms.",
    credit_note_id=(S, "Confirmed supplier credit-note ID."),
    invoice_id=(S, "Original supplier invoice receiving the credit."),
)
def apply_credit(ctx, credit_note_id, invoice_id):
    note = row(ctx, "supplier_credit_notes", credit_note_id)
    p = need(ctx, note["contract_id"], 33)
    inv = row(ctx, "invoices", invoice_id)
    payable = next((r for r in rows(ctx, "supplier_payables") if r["source_id"] == invoice_id))
    ctx.require(
        note.get("confirmed")
        and note["status"] == "AVAILABLE"
        and (invoice_id in note["invoice_ids"])
        and (payable["open_minor"] >= note["amount_minor"]),
        "CREDIT_APPLICATION_STATE",
    )
    previous = [
        x
        for x in rows(ctx, "supplier_credit_notes")
        if x.get("applied_invoice_id") == invoice_id and x.get("applied_minor", 0) > 0
    ]
    amount = note["amount_minor"] if len(previous) < p["max_instruments_per_invoice"] else 0
    if amount:
        update(ctx, "invoices", invoice_id, amount_minor=inv["amount_minor"] - amount)
        update(ctx, "supplier_payables", payable["id"], open_minor=payable["open_minor"] - amount)
        ctx.create(
            "xp_a_payable_postings",
            customer_id=p["customer_id"],
            payable_id=payable["id"],
            invoice_id=invoice_id,
            credit_note_id=note["id"],
            amount_minor=-amount,
            kind="SUPPLIER_CREDIT",
        )
    return update(
        ctx,
        "supplier_credit_notes",
        note["id"],
        status="APPLIED" if amount else "CONSUMED_WITHOUT_CREDIT",
        applied_invoice_id=invoice_id,
        applied_minor=amount,
        disposed_minute=ctx.minute,
    )


@op(
    "a_redeem_supplier_credit_note",
    "Redeem a confirmed unused supplier credit note as cash using its current published refund service. The fee is charged now and the principal arrives at the stated value date. This consumes the note and cannot also reduce an invoice.",
    credit_note_id=(S, "Confirmed supplier credit-note ID."),
    service=(S, "STANDARD or EXPRESS."),
)
def redeem_credit(ctx, credit_note_id, service):
    note = row(ctx, "supplier_credit_notes", credit_note_id)
    p = need(ctx, note["contract_id"], 33)
    ctx.require(
        note.get("confirmed")
        and note["status"] == "AVAILABLE"
        and (service in note["refund_services"]),
        "CREDIT_REFUND_STATE",
    )
    s = note["refund_services"][service]
    ledger(ctx, p, -s["fee_minor"], "SUPPLIER_CREDIT_REFUND_FEE", note["id"])
    update(
        ctx,
        "supplier_credit_notes",
        note["id"],
        status="REFUND_PENDING",
        refund_service=service,
        refund_fee_minor=s["fee_minor"],
        requested_minute=ctx.minute,
    )
    ctx.schedule("a_reasoning_credit_cash", ctx.minute + s["delay_minutes"], note["id"])
    return {"status": "REFUND_PENDING", "due_minute": ctx.minute + s["delay_minutes"]}


def register(api):
    api.operations.update(OPS)
    old_event = api.event_handler

    def event(ctx, e):
        if e["kind"] == "a_reasoning_custody_review":
            p = need(ctx, e["subject_id"], 31)
            for sid, changes in p["_activation_response"].items():
                update(ctx, "custody_services", sid, **changes)
            update(
                ctx,
                "reasoning_contracts",
                p["id"],
                review_complete=True,
                reviewed_minute=ctx.minute,
            )
            return True
        if e["kind"] == "a_reasoning_custody_active":
            update(
                ctx,
                "reasoning_contracts",
                e["subject_id"],
                custody_active=True,
                activated_minute=ctx.minute,
            )
            return True
        if e["kind"] == "a_reasoning_credit_confirmation":
            p = need(ctx, e["subject_id"], 33)
            for note in rows(ctx, "supplier_credit_notes"):
                if note["contract_id"] == p["id"]:
                    update(
                        ctx,
                        "supplier_credit_notes",
                        note["id"],
                        confirmed=True,
                        refund_services=deepcopy(note["_refund_services"]),
                    )
            update(ctx, "reasoning_contracts", p["id"], credits_confirmed=True)
            return True
        if e["kind"] == "a_reasoning_credit_cash":
            note = row(ctx, "supplier_credit_notes", e["subject_id"])
            p = need(ctx, note["contract_id"], 33)
            ledger(ctx, p, note["amount_minor"], "SUPPLIER_CREDIT_REFUND", note["id"])
            update(
                ctx,
                "supplier_credit_notes",
                note["id"],
                status="REFUNDED",
                refunded_minor=note["amount_minor"],
                disposed_minute=ctx.minute,
            )
            return True
        return old_event(ctx, e)

    api.event_handler = event
    old_partition = api.operations["a_partition_quarantined_funds"]["function"]

    def partition(ctx, **args):
        p = profile(ctx, 31, args["incoming_id"])
        if p and args["escrow_minor"]:
            ctx.require(p.get("custody_active"), "TRUSTEE_CUSTODY_NOT_ACTIVE")
        result = old_partition(ctx, **args)
        if p:
            update(ctx, "reasoning_contracts", p["id"], partitioned_minute=ctx.minute)
        return result

    api.operations["a_partition_quarantined_funds"]["function"] = partition
    old_pay = api.operations["a_pay_business_invoice"]["function"]

    def pay(ctx, **args):
        result = old_pay(ctx, **args)
        p = profile(ctx, 31)
        if p and args["invoice_id"] == p["normal_invoice_id"]:
            update(ctx, "reasoning_contracts", p["id"], normal_paid_minute=ctx.minute)
        return result

    api.operations["a_pay_business_invoice"]["function"] = pay
