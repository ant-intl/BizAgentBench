"""Origin-bound tax credit lots, two original liability periods and delayed cash refunds."""

TABLE = "xp_b_reasoning_tax_cycle"
LOTS = "xp_b_reasoning_tax_credit_lots"
ALLOC = "xp_b_reasoning_tax_allocations"


def context(ctx, claim_id=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (claim_id is None or r["claim_id"] == claim_id)
        ),
        None,
    )


def register(operations, old_event):
    old = operations["dispose_tax_credit"]["function"]

    def dispose(ctx, claim_id, mode, amount_minor, credit_lot_id=None, period_id=None):
        r = context(ctx, claim_id)
        if not r:
            return old(ctx, claim_id=claim_id, mode=mode, amount_minor=amount_minor)
        claim = ctx.record("xp_b_tax_claims", claim_id)
        ctx.require(credit_lot_id is not None, "CREDIT_ORIGIN_REQUIRED")
        lot = ctx.record(LOTS, credit_lot_id)
        ctx.require(
            lot["claim_id"] == claim_id
            and mode in ("cash", "offset")
            and (0 < amount_minor <= lot["available_minor"]),
            "CREDIT_LOT_BALANCE",
        )
        period_id = period_id or claim["next_id"]
        if mode == "offset":
            p = ctx.record("xp_b_next_tax", period_id)
            ctx.require(
                period_id in lot["allowed_period_ids"]
                and amount_minor <= p["amount_minor"] - p["paid_minor"] - p["offset_minor"],
                "CREDIT_PERIOD_ELIGIBILITY",
            )
            ctx.require(
                p["offset_minor"] + amount_minor <= r["offset_ceilings"][period_id],
                "PERIOD_OFFSET_CEILING",
            )
            fields = dict(offset_minor=p["offset_minor"] + amount_minor)
            if p["paid_minor"] + fields["offset_minor"] == p["amount_minor"]:
                fields["paid_minute"] = ctx.minute
            ctx.update("xp_b_next_tax", period_id, **fields)
        else:
            ctx.schedule(
                "b_tax_cash",
                ctx.minute + claim["refund_delay"],
                claim_id,
                _amount_minor=amount_minor,
            )
        ctx.update(LOTS, credit_lot_id, available_minor=lot["available_minor"] - amount_minor)
        ctx.update(
            "xp_b_tax_claims",
            claim_id,
            available_minor=claim["available_minor"] - amount_minor,
            **{mode + "_minor": claim.get(mode + "_minor", 0) + amount_minor},
        )
        return ctx.create(
            ALLOC,
            customer_id=r["customer_id"],
            claim_id=claim_id,
            credit_lot_id=credit_lot_id,
            mode=mode,
            period_id=period_id if mode == "offset" else None,
            amount_minor=amount_minor,
            minute=ctx.minute,
        )

    operations["dispose_tax_credit"]["function"] = dispose
    operations["dispose_tax_credit"]["parameters"]["properties"].update(
        credit_lot_id={"type": "string"}, period_id={"type": "string"}
    )
    operations["dispose_tax_credit"][
        "description"
    ] += " Enrolled consecutive-period schedules require credit_lot_id and accept period_id for offset; actual lot eligibility and cumulative period ceilings apply."

    def event(ctx, e):
        result = old_event(ctx, e)
        if e["kind"] == "b_tax_credit":
            r = context(ctx, e["subject_id"])
            if r:
                claim = ctx.record("xp_b_tax_claims", e["subject_id"])
                ctx.create(
                    LOTS,
                    customer_id=r["customer_id"],
                    claim_id=claim["id"],
                    origin="initial_review",
                    approved_minor=claim["approved_minor"],
                    available_minor=claim["approved_minor"],
                    allowed_period_ids=r["period_ids"],
                    published_minute=ctx.minute,
                )
        elif e["kind"] == "b_enhance_tax_appeal":
            ext = ctx.record("xp_b_tax_appeals", e["subject_id"])
            r = context(ctx, ext["claim_id"])
            if r:
                ctx.create(
                    LOTS,
                    customer_id=r["customer_id"],
                    claim_id=r["claim_id"],
                    origin="appeal",
                    approved_minor=ext["additional_credit_minor"],
                    available_minor=ext["additional_credit_minor"],
                    allowed_period_ids=r["appeal_period_ids"],
                    published_minute=ctx.minute,
                )
        return result

    return event
