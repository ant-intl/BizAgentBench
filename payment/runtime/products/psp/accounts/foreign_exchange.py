"""Early supplier installments, late credit apportionment and real FX stock."""

TABLE = "xp_b_reasoning_supplier_schedule"
PAY = "xp_b_reasoning_fx_installments"
OPS = {}
S = {"type": "string"}
I = {"type": "integer", "minimum": 0}


def context(ctx, requirement_id=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (requirement_id is None or r["requirement_id"] == requirement_id)
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
    "pay_supplier_foreign_installment",
    "Pay a positive part of the same supplier invoice from actual acquired foreign currency. It reduces both held currency and the remaining original invoice, and cannot be recalled. It does not cancel a still-open FX order or anticipate unconfirmed supplier credit.",
    {"requirement_id": S, "amount_minor": I},
)
def installment(ctx, requirement_id, amount_minor):
    r = context(ctx, requirement_id)
    q = ctx.record("xp_b_fx_requirements", requirement_id)
    ctx.require(
        r is not None
        and (not q["paid"])
        and (0 < amount_minor <= min(q["acquired_minor"], q["need_minor"])),
        "SUPPLIER_INSTALLMENT_BALANCE",
    )
    ctx.update(
        "xp_b_fx_requirements",
        q["id"],
        acquired_minor=q["acquired_minor"] - amount_minor,
        need_minor=q["need_minor"] - amount_minor,
    )
    return ctx.create(
        PAY,
        customer_id=r["customer_id"],
        requirement_id=requirement_id,
        amount_minor=amount_minor,
        received_minute=ctx.minute,
    )


@op(
    "allocate_supplier_credit_portions",
    "Apportion confirmed supplier credit between invoice offset and cash refund. Their sum must equal the actual credit advice. Offset cannot exceed this invoice’s remaining unpaid principal after actual installments. Cash refund has the published delay and one refund fee; neither a future-contract credit nor a concession satisfies these two dispositions.",
    {"credit_record_id": S, "offset_minor": I, "refund_minor": I},
)
def portions(ctx, credit_record_id, offset_minor, refund_minor):
    credit = ctx.record("xp_b_supplier_fx_credits", credit_record_id)
    r = context(ctx, credit["requirement_id"])
    q = ctx.record("xp_b_fx_requirements", credit["requirement_id"])
    ctx.require(
        r is not None
        and credit.get("advice_received")
        and (not credit.get("credit_applied"))
        and (offset_minor + refund_minor == credit["credit_minor"])
        and (offset_minor <= q["need_minor"]),
        "SUPPLIER_CREDIT_PORTIONS",
    )
    ctx.update("xp_b_fx_requirements", q["id"], need_minor=q["need_minor"] - offset_minor)
    result = ctx.update(
        "xp_b_supplier_fx_credits",
        credit["id"],
        credit_applied=True,
        credit_choice="split",
        applied_credit_minor=offset_minor,
        cash_elected_minor=refund_minor,
        refund_fee_charged_minor=credit["refund_fee_minor"] if refund_minor else 0,
    )
    if refund_minor:
        source_gross = refund_minor * q["rate_num"] // q["rate_den"]
        ctx.schedule(
            "b_reasoning_supplier_refund",
            ctx.minute + credit["refund_delay"],
            credit["id"],
            amount_minor=max(0, source_gross - credit["refund_fee_minor"]),
        )
    return result


def register(operations, old_event):
    operations.update(OPS)
    old_credit = operations["allocate_supplier_invoice_credit"]["function"]
    old_confirm = operations["confirm_supplier_fx_settlement"]["function"]
    old_payment = operations["pay_foreign_obligation"]["function"]

    def payment(ctx, requirement_id):
        result = old_payment(ctx, requirement_id=requirement_id)
        if context(ctx, requirement_id):
            result = ctx.update("xp_b_fx_requirements", requirement_id, paid_minute=ctx.minute)
        return result

    operations["pay_foreign_obligation"]["function"] = payment

    def allocate(ctx, record_id, choice):
        credit = ctx.record("xp_b_supplier_fx_credits", record_id)
        r = context(ctx, credit["requirement_id"])
        if r and choice in ("invoice_netting", "cash_refund"):
            q = ctx.record("xp_b_fx_requirements", r["requirement_id"])
            offset = (
                min(q["need_minor"], credit["credit_minor"]) if choice == "invoice_netting" else 0
            )
            return portions(ctx, record_id, offset, credit["credit_minor"] - offset)
        return old_credit(ctx, record_id=record_id, choice=choice)

    operations["allocate_supplier_invoice_credit"]["function"] = allocate

    def confirm(ctx, record_id):
        credit = ctx.record("xp_b_supplier_fx_credits", record_id)
        r = context(ctx, credit["requirement_id"])
        if r:
            q = ctx.record("xp_b_fx_requirements", r["requirement_id"])
            order = ctx.record("xp_b_fx_orders", r["order_id"])
            if (
                q["need_minor"] == 0
                and q["acquired_minor"] == 0
                and (order["status"] == "cancelled")
            ):
                ctx.update(
                    "xp_b_fx_requirements",
                    q["id"],
                    paid=True,
                    remaining_minor=0,
                    paid_minute=ctx.minute,
                )
        return old_confirm(ctx, record_id=record_id)

    operations["confirm_supplier_fx_settlement"]["function"] = confirm

    def event(ctx, e):
        if e["kind"] == "b_reasoning_supplier_refund":
            credit = ctx.record("xp_b_supplier_fx_credits", e["subject_id"])
            amount = e["_payload"]["amount_minor"]
            ctx.ledger(
                credit["account_id"], amount, "USD", "supplier_credit_cash_refund", credit["id"]
            )
            ctx.update(
                "xp_b_supplier_fx_credits",
                credit["id"],
                refund_received_minor=amount,
                refund_received_minute=ctx.minute,
            )
            return True
        return old_event(ctx, e)

    return event
