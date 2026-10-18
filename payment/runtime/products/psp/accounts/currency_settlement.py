"""Partial PvP against real reserved source cash and published reverse-leg receipts."""

from copy import deepcopy

TABLE = "xp_b_reasoning_pvp_schedule"
TRADES = "xp_b_reasoning_pvp_trades"


def context(ctx, pair_id=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (pair_id is None or pair_id in r["dealers"])
        ),
        None,
    )


def register(operations, old_event):
    old_prepare = operations["prepare_currency_leg"]["function"]
    old_settle = operations["settle_currency_pair"]["function"]
    old_release = operations["release_currency_preparations"]["function"]
    old_confirm = operations["confirm_currency_settlement_finality"]["function"]

    def prepare(ctx, pair_id, leg, amount_minor=None):
        r = context(ctx, pair_id)
        if not r:
            return old_prepare(ctx, pair_id=pair_id, leg=leg)
        p = ctx.record("xp_b_settlement_pairs", pair_id)
        d = r["dealers"][pair_id]
        ctx.require(p["status"] == "open", "PREPARATION_CLOSED")
        if leg == "counterparty":
            ctx.require(p.get("source_hold_minor", 0) > 0, "LIVE_SOURCE_PLEDGE_REQUIRED")
        out = old_prepare(ctx, pair_id=pair_id, leg=leg)
        if leg == "source":
            amount = amount_minor if amount_minor is not None else p["source_minor"]
            ctx.require(0 < amount <= p["source_minor"], "SOURCE_HOLD_AMOUNT")
            ctx.ledger(p["account_id"], -amount, p["currency"], "pvp_source_hold", pair_id)
            ctx.update(
                "xp_b_settlement_pairs",
                pair_id,
                source_hold_minor=amount,
                source_held_minute=ctx.minute,
            )
        else:
            ctx.require(not p.get("reverse_requested"), "REVERSE_ALREADY_REQUESTED")
            ctx.update(
                "xp_b_settlement_pairs",
                pair_id,
                counterparty_prepared=False,
                reverse_requested=True,
            )
            if d["delay_minutes"]:
                ctx.schedule(
                    "b_reasoning_pvp_reverse",
                    ctx.minute + d["delay_minutes"],
                    pair_id,
                    schedule_id=r["id"],
                )
            else:
                ctx.update(
                    "xp_b_settlement_pairs",
                    pair_id,
                    reverse_received=True,
                    counterparty_prepared=True,
                    reverse_capacity_minor=r["_reverse_results"][pair_id],
                    reverse_remaining_minor=r["_reverse_results"][pair_id],
                    reverse_minute=ctx.minute,
                )
        return ctx.record("xp_b_settlement_pairs", pair_id)

    def settle(ctx, pair_id, mode, source_minor=None):
        r = context(ctx, pair_id)
        if not r:
            return old_settle(ctx, pair_id=pair_id, mode=mode)
        p = ctx.record("xp_b_settlement_pairs", pair_id)
        window = ctx.record("xp_b_currency_windows", r["window_id"])
        amount = source_minor if source_minor is not None else p["source_minor"]
        ctx.require(
            mode in ("synchronous", "unilateral")
            and window.get("assigned_pair_id") == pair_id
            and (ctx.minute <= window.get("expires_minute", -1)),
            "PVP_WINDOW",
        )
        ctx.require(0 < amount <= p.get("source_hold_minor", 0), "PVP_SOURCE_HOLD")
        if mode == "synchronous":
            ctx.require(
                p.get("reverse_received") and amount <= p.get("reverse_remaining_minor", 0),
                "PVP_REVERSE_SHORTFALL",
            )
        fee = (
            p["sync_fee_minor"] + amount * r["dealers"][pair_id]["execution_rate_bps"] // 10000
            if mode == "synchronous"
            else 0
        )
        ctx.ledger(p["account_id"], -fee, p["currency"], "pvp_execution_fee", pair_id)
        delivered = (
            amount * p["target_minor"] // p["source_minor"]
            if p.get("reverse_received") and amount <= p.get("reverse_remaining_minor", 0)
            else 0
        )
        remain = p["source_hold_minor"] - amount
        ctx.update(
            "xp_b_settlement_pairs",
            pair_id,
            source_hold_minor=remain,
            executed_source_minor=p.get("executed_source_minor", 0) + amount,
            received_minor=p.get("received_minor", 0) + delivered,
            exposure_minor=p.get("exposure_minor", 0) + (amount if mode == "unilateral" else 0),
            reverse_remaining_minor=max(0, p.get("reverse_remaining_minor", 0) - amount),
            status="settled" if not remain else "partially_settled",
            source_prepared=bool(remain),
            counterparty_prepared=bool(remain),
            mode=mode,
        )
        trade = ctx.create(
            TRADES,
            customer_id=r["customer_id"],
            schedule_id=r["id"],
            pair_id=pair_id,
            source_minor=amount,
            target_minor=delivered,
            mode=mode,
            minute=ctx.minute,
            fee_minor=fee,
            reverse_capacity_minor=p.get("reverse_capacity_minor", 0),
        )
        ctx.emit(
            "pvp_slice_executed",
            pair_id=pair_id,
            source_minor=amount,
            target_minor=delivered,
            mode=mode,
        )
        return trade

    def release(ctx, pair_id):
        r = context(ctx, pair_id)
        if not r:
            return old_release(ctx, pair_id=pair_id)
        p = ctx.record("xp_b_settlement_pairs", pair_id)
        held = p.get("source_hold_minor", 0)
        ctx.require(held > 0 or p.get("counterparty_prepared"), "NO_PREPARATION_TO_RELEASE")
        if held:
            ctx.ledger(p["account_id"], held, p["currency"], "pvp_hold_release", pair_id)
        return ctx.update(
            "xp_b_settlement_pairs",
            pair_id,
            source_hold_minor=0,
            source_prepared=False,
            counterparty_prepared=False,
            status="settled" if p.get("executed_source_minor", 0) else "released",
            released_minor=p.get("released_minor", 0) + held,
        )

    def confirm(ctx, record_id):
        r = context(ctx)
        if not r or r["window_id"] != record_id:
            return old_confirm(ctx, record_id=record_id)
        trades = ctx.records(TRADES, schedule_id=r["id"])
        ctx.require(bool(trades), "NO_SETTLED_SLICES")
        return ctx.update(
            TABLE,
            r["id"],
            finality_confirmed=True,
            source_confirmed_minor=sum((x["source_minor"] for x in trades)),
            target_confirmed_minor=sum((x["target_minor"] for x in trades)),
            held_confirmed_minor=sum(
                (
                    ctx.record("xp_b_settlement_pairs", p).get("source_hold_minor", 0)
                    for p in r["dealers"]
                )
            ),
        )

    for name, fn in [
        ("prepare_currency_leg", prepare),
        ("settle_currency_pair", settle),
        ("release_currency_preparations", release),
        ("confirm_currency_settlement_finality", confirm),
    ]:
        operations[name]["function"] = fn
    operations["prepare_currency_leg"]["parameters"]["properties"]["amount_minor"] = {
        "type": "integer",
        "minimum": 1,
    }
    operations["prepare_currency_leg"][
        "description"
    ] += " Enrolled partial PvP schedules allow amount_minor on source preparation, moving that exact principal into a real segregated hold. Counterparty preparation confirms actual capacity asynchronously."
    operations["settle_currency_pair"]["parameters"]["properties"]["source_minor"] = {
        "type": "integer",
        "minimum": 1,
    }
    operations["settle_currency_pair"][
        "description"
    ] += " Enrolled partial PvP schedules allow source_minor for a slice, consuming source hold and actual confirmed reverse capacity; see partial settlement terms."

    def event(ctx, e):
        if e["kind"] == "b_reasoning_pvp_reverse":
            r = ctx.record(TABLE, e["_payload"]["schedule_id"])
            amount = r["_reverse_results"][e["subject_id"]]
            pair = ctx.record("xp_b_settlement_pairs", e["subject_id"])
            live = pair.get("source_hold_minor", 0) > 0 and pair["status"] in (
                "open",
                "partially_settled",
            )
            ctx.update(
                "xp_b_settlement_pairs",
                e["subject_id"],
                reverse_received=True,
                counterparty_prepared=live,
                reverse_capacity_minor=amount,
                reverse_remaining_minor=amount if live else 0,
                reverse_minute=ctx.minute,
                reverse_status="allocated" if live else "closed_without_commitment",
            )
            return
        return old_event(ctx, e)

    return event
