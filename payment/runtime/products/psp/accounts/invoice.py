"""Early fee invoice delivery and later line-bound credit documents."""

from copy import deepcopy

TABLE = "xp_b_reasoning_invoice_schedule"
NOTES = "xp_b_reasoning_fee_credit_notes"
OPS = {}
S = {"type": "string"}
I = {"type": "integer", "minimum": 1}


def context(ctx, company=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (company is None or r["company"] == company)
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
    "issue_fee_invoice_credit_note",
    "Issue and deliver a negative adjustment tied to one line of a delivered original fee invoice. It does not free that line’s invoice occupancy. The transport service accepts an amount within the original line, charges the public note fee, and snapshots actual buyer acceptance. The refund and line identity still determine whether the note is economically justified; another fee’s refund is not interchangeable.",
    {"invoice_id": S, "fee_id": S, "amount_minor": I},
)
def note(ctx, invoice_id, fee_id, amount_minor):
    invoice = ctx.record("xp_b_invoices", invoice_id)
    r = context(ctx, invoice["company"])
    fee = ctx.record("xp_b_fees", fee_id)
    ctx.require(
        r is not None
        and r.get("buyer_format_received")
        and (invoice["status"] == "delivered")
        and (fee_id in invoice["fee_ids"]),
        "CREDIT_NOTE_PARENT",
    )
    used = sum(
        (
            n["amount_minor"]
            for n in ctx.records(NOTES, invoice_id=invoice_id)
            if n["fee_id"] == fee_id and n["status"] == "delivered"
        )
    )
    ctx.require(
        0 < amount_minor <= invoice["line_amounts"][fee_id] - used, "CREDIT_NOTE_LINE_AMOUNT"
    )
    ctx.ledger(
        r["account_id"], -r["credit_note_fee_minor"], "USD", "invoice_credit_note_fee", invoice_id
    )
    return ctx.create(
        NOTES,
        customer_id=r["customer_id"],
        invoice_id=invoice_id,
        fee_id=fee_id,
        amount_minor=amount_minor,
        company=invoice["company"],
        address=invoice["address"],
        status="delivered",
        delivered_minute=ctx.minute,
        buyer_format_accepted=r["credit_note_supported"],
    )


def register(operations, old_event):
    operations.update(OPS)
    old_issue = operations["issue_fee_invoice"]["function"]
    old_void = operations["void_fee_invoice"]["function"]
    old_deliver = operations["deliver_fee_invoice"]["function"]

    def issue(ctx, profile_id, fee_ids):
        invoice = old_issue(ctx, profile_id=profile_id, fee_ids=fee_ids)
        if context(ctx, invoice["company"]):
            ctx.update(
                "xp_b_invoices",
                invoice["id"],
                line_amounts={
                    i: ctx.record("xp_b_fees", i)["amount_minor"]
                    - ctx.record("xp_b_fees", i).get("refunded_minor", 0)
                    for i in fee_ids
                },
                issued_minute=ctx.minute,
            )
        return invoice

    operations["issue_fee_invoice"]["function"] = issue

    def deliver(ctx, invoice_id):
        invoice = old_deliver(ctx, invoice_id=invoice_id)
        if context(ctx, invoice["company"]):
            ctx.update("xp_b_invoices", invoice_id, delivered_minute=ctx.minute)
        return invoice

    operations["deliver_fee_invoice"]["function"] = deliver

    def void(ctx, invoice_id):
        result = old_void(ctx, invoice_id=invoice_id)
        if context(ctx, ctx.record("xp_b_invoices", invoice_id)["company"]):
            for n in ctx.records(NOTES, invoice_id=invoice_id):
                ctx.update(NOTES, n["id"], status="void", voided_minute=ctx.minute)
        return result

    operations["void_fee_invoice"]["function"] = void

    def event(ctx, e):
        result = old_event(ctx, e)
        if e["kind"] == "b_fee_refund":
            r = context(ctx, ctx.record("xp_b_fees", e["subject_id"])["company"])
            if r:
                ctx.update(
                    TABLE,
                    r["id"],
                    buyer_format_received=True,
                    credit_note_supported=r["_credit_note_supported"],
                    format_minute=ctx.minute,
                )
        return result

    return event
