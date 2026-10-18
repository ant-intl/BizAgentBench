"""Finite fee investigations reserve actual review capacity across original bills."""

TABLE = "xp_b_reasoning_billing_review"
BOOK = "xp_b_reasoning_billing_files"


def context(ctx, fee_id=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (fee_id is None or fee_id in r["fee_ids"])
        ),
        None,
    )


def book(ctx, schedule_id, channel_id, fee_ids):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(
        context(ctx) is not None
        and channel_id in r["channels"]
        and (len(fee_ids) == len(set(fee_ids)) > 0),
        "INVESTIGATION_SCOPE",
    )
    ch = r["channels"][channel_id]
    ctx.require(
        set(fee_ids) <= set(r["fee_ids"]) and len(fee_ids) <= ch["max_lines"], "INVESTIGATION_LINES"
    )
    ctx.require(set(fee_ids) <= set(ch["eligible_fee_ids"]), "INVESTIGATION_EVIDENCE_FORMAT")
    used = sum((x["units"] for x in ctx.records(BOOK, schedule_id=schedule_id)))
    ctx.require(used + ch["units"] <= r["capacity_units"], "INVESTIGATION_CAPACITY")
    ctx.ledger(r["account_id"], -ch["fee_minor"], "USD", "billing_investigation_fee", schedule_id)
    return ctx.create(
        BOOK,
        customer_id=r["customer_id"],
        schedule_id=schedule_id,
        channel_id=channel_id,
        fee_ids=list(fee_ids),
        units=ch["units"],
        fee_minor=ch["fee_minor"],
        booked_minute=ctx.minute,
    )


def register(operations, old_event):
    operations["reserve_fee_investigation"] = dict(
        description="Open one paid investigation file through channel_id for actual fee_ids. Published eligibility, per-file line capacity, units and fee apply. This irreversibly reserves shared current-cycle review units but does not request any refund or alter fee basis.",
        parameters=dict(
            type="object",
            properties={
                "schedule_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "fee_ids": {"type": "array", "items": {"type": "string"}},
            },
            required=["schedule_id", "channel_id", "fee_ids"],
            additionalProperties=False,
        ),
        read_only=False,
        function=book,
    )
    old = operations["request_fee_correction"]["function"]
    old_delivery = operations["deliver_fee_recovery_statement"]["function"]

    def correct(ctx, fee_id, amount_minor):
        r = context(ctx, fee_id)
        if r:
            ctx.require(
                any((fee_id in f["fee_ids"] for f in ctx.records(BOOK, schedule_id=r["id"]))),
                "INVESTIGATION_FILE_REQUIRED",
            )
        result = old(ctx, fee_id=fee_id, amount_minor=amount_minor)
        if r:
            ctx.update(
                "xp_b_billing",
                fee_id,
                correction_minute=ctx.minute,
                claimed_minor=amount_minor,
                basis_volume_at_correction=result["eligible_volume_minor"],
            )
        return result

    def delivery(ctx, record_id, choice):
        result = old_delivery(ctx, record_id=record_id, choice=choice)
        r = context(ctx)
        if r:
            ctx.update(TABLE, r["id"], statement_delivered_minute=ctx.minute)
        return result

    operations["request_fee_correction"]["function"] = correct
    operations["request_fee_correction"][
        "description"
    ] += " Enrolled billing review schedules additionally require a previously reserved investigation file covering this exact fee."
    operations["deliver_fee_recovery_statement"]["function"] = delivery
    return old_event
