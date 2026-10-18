"""One prepaid campaign, distinct channel obligations and costly reallocation."""

from copy import deepcopy

TABLE = "xp_b_reasoning_campaign_mix"
OPS = {}
S = {"type": "string"}
I = {"type": "integer", "minimum": 1}


def context(ctx, launch_id=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            r
            for r in ctx.records(TABLE)
            if r["customer_id"] == ctx.customer_id
            and r.get("reasoning_version") == "b-reasoning-1"
            and (launch_id is None or r["launch_id"] == launch_id)
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
    "commit_campaign_allocation_offer",
    "Commit one advertised allocation offer for the original Summer launch. The service reservation fee is paid now; the subsequent original capacity request publishes actual accepted channel amounts within the offer ranges. The offer is not a promise that every channel’s customer minimum is satisfied.",
    {"schedule_id": S, "offer_id": S},
)
def commit(ctx, schedule_id, offer_id):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(
        context(ctx, r["launch_id"]) is not None
        and (not r.get("offer_id"))
        and (offer_id in r["offers"]),
        "CAMPAIGN_OFFER_STATE",
    )
    offer = r["offers"][offer_id]
    ctx.ledger(r["account_id"], -offer["fee_minor"], "USD", "campaign_allocation_offer", r["id"])
    return ctx.update(TABLE, r["id"], offer_id=offer_id, committed_minute=ctx.minute)


@op(
    "cancel_accepted_campaign_channel",
    "Cancel unactivated accepted capacity from one channel. The contract cancellation percentage is lost from actual prepaid wallet credit; the remaining capacity becomes reusable. This cannot recall advertising already activated.",
    {"schedule_id": S, "channel": S, "amount_minor": I},
)
def cancel(ctx, schedule_id, channel, amount_minor):
    r = ctx.record(TABLE, schedule_id)
    launch = ctx.record("xp_b_campaign_launches", r["launch_id"])
    wallet = ctx.record("xp_b_business_accounts", launch["wallet_id"])
    ctx.require(
        r.get("allocation_received")
        and (not launch.get("activated"))
        and (0 < amount_minor <= r["accepted_by_channel"].get(channel, 0)),
        "ACCEPTED_CHANNEL_CANCELLATION",
    )
    loss = amount_minor * r["cancellation_bps"] // 10000
    mix = deepcopy(r["accepted_by_channel"])
    mix[channel] -= amount_minor
    ctx.update(
        "xp_b_business_accounts",
        wallet["id"],
        usable_minor=wallet["usable_minor"] - loss,
        cancellation_loss_minor=wallet.get("cancellation_loss_minor", 0) + loss,
    )
    ctx.update(
        "xp_b_campaign_launches",
        launch["id"],
        accepted_capacity_minor=sum(mix.values()),
        free_capacity_minor=launch.get("free_capacity_minor", 0) + amount_minor - loss,
    )
    ctx.emit(
        "campaign_capacity_cancelled",
        subject_id=r["id"],
        channel=channel,
        amount_minor=amount_minor,
        credit_lost_minor=loss,
    )
    return ctx.update(
        TABLE,
        r["id"],
        accepted_by_channel=mix,
        cancelled_minor=r.get("cancelled_minor", 0) + amount_minor,
        cancellation_loss_minor=r.get("cancellation_loss_minor", 0) + loss,
    )


def register(operations, old_event):
    operations.update(OPS)
    originals = {
        k: operations[k]["function"]
        for k in (
            "reserve_campaign_launch_capacity",
            "book_campaign_makegood",
            "activate_campaign_launch",
        )
    }

    def reserve(ctx, record_id):
        r = context(ctx, record_id)
        if r:
            ctx.require(r.get("offer_id"), "ALLOCATION_OFFER_REQUIRED")
            ctx.update(
                "xp_b_campaign_launches",
                record_id,
                _accepted_capacity_minor=sum(r["_allocation_results"][r["offer_id"]].values()),
            )
        return originals["reserve_campaign_launch_capacity"](ctx, record_id=record_id)

    operations["reserve_campaign_launch_capacity"]["function"] = reserve

    def book(ctx, record_id, choice):
        r = context(ctx, record_id)
        if r:
            l = ctx.record("xp_b_campaign_launches", record_id)
            wallet = ctx.record("xp_b_business_accounts", l["wallet_id"])
            ctx.update(
                "xp_b_campaign_launches",
                record_id,
                free_capacity_minor=wallet["usable_minor"] - l["accepted_capacity_minor"],
            )
        result = originals["book_campaign_makegood"](ctx, record_id=record_id, choice=choice)
        if r:
            l = ctx.record("xp_b_campaign_launches", record_id)
            ch = r["placements"][choice]
            ctx.update(
                TABLE,
                r["id"],
                makegood_channel=ch["channel"],
                makegood_delay=ch["delay_minutes"],
                makegood_minor=l["makegood_minor"],
                makegood_campaign=l["makegood_campaign"],
            )
        return result

    operations["book_campaign_makegood"]["function"] = book

    def activate(ctx, record_id):
        r = context(ctx, record_id)
        result = originals["activate_campaign_launch"](ctx, record_id=record_id)
        if r:
            receipts = [
                dict(
                    channel=channel,
                    amount_minor=amount,
                    received_minute=ctx.minute,
                    campaign=ctx.record("xp_b_campaign_launches", record_id)["campaign"],
                )
                for (channel, amount) in r["accepted_by_channel"].items()
                if amount
            ]
            ctx.update(TABLE, r["id"], receipts=receipts, activation_minute=ctx.minute)
            if r.get("makegood_minor"):
                ctx.schedule(
                    "b_reasoning_campaign_delivery", ctx.minute + r["makegood_delay"], r["id"]
                )
        return result

    operations["activate_campaign_launch"]["function"] = activate

    def event(ctx, e):
        if e["kind"] == "b_reasoning_campaign_delivery":
            r = ctx.record(TABLE, e["subject_id"])
            receipts = deepcopy(r.get("receipts", []))
            receipts.append(
                dict(
                    channel=r["makegood_channel"],
                    amount_minor=r["makegood_minor"],
                    campaign=r["makegood_campaign"],
                    received_minute=ctx.minute,
                )
            )
            ctx.update(TABLE, r["id"], receipts=receipts, makegood_received=True)
            return True
        result = old_event(ctx, e)
        if e["kind"] == "b_enhance_capacity":
            r = context(ctx, e["subject_id"])
            if r:
                ctx.update(
                    TABLE,
                    r["id"],
                    allocation_received=True,
                    accepted_by_channel=deepcopy(r["_allocation_results"][r["offer_id"]]),
                    allocation_minute=ctx.minute,
                )
        return result

    return event
