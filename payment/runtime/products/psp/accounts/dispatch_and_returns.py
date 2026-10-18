"""Product-specific dependencies for protected goods, order fulfilment and platform exit."""

from copy import deepcopy

OPS = {}
TABLE = "xp_b_delivery_schedules"
STOCK = "xp_b_dispatch_stock"
LINES = "xp_b_dispatch_lines"
BOOK = "xp_b_dispatch_bookings"
TRANSFERS = "xp_b_platform_transfers"


def op(name, description, **fields):

    def decorate(fn):
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object",
                properties={
                    k: dict(
                        type=t,
                        description=d,
                        **{"items": {"type": "string"}} if t == "array" else {},
                    )
                    for (k, (t, d)) in fields.items()
                },
                required=list(fields),
                additionalProperties=False,
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return decorate


def p(ctx, n):
    return next((r for r in ctx.records(TABLE) if r["number"] == n), None)


def upd(ctx, r, **kw):
    return ctx.update(TABLE, r["id"], **kw)


def charge(ctx, r, amount, kind):
    if amount:
        ctx.ledger(r["account_id"], -amount, "USD", kind, r["id"])


@op(
    "book_equipment_commissioning",
    "Reserve a listed commissioning service before its booking deadline. The fee is nonrefundable; the reservation does not inspect the equipment or replace missing components.",
    schedule_id=("string", "Equipment performance schedule."),
    service=("string", "Listed commissioning service key."),
)
def book_install(ctx, schedule_id, service):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(
        r["number"] == -34 and (not r.get("booking")) and (service in r["services"]),
        "INSTALLATION_BOOKING",
    )
    s = r["services"][service]
    ctx.require(ctx.minute <= s["book_until"], "INSTALLER_BOOKING_CLOSED")
    charge(ctx, r, s["fee_minor"], "commissioning_booking")
    return upd(ctx, r, booking=service, booked_minute=ctx.minute)


@op(
    "commission_received_equipment",
    "Start the booked installation only after the original paid equipment and any replacement kit have been accepted. Completion follows the service delay; accepting equipment is not commissioning it.",
    schedule_id=("string", "Equipment performance schedule."),
)
def commission(ctx, schedule_id):
    r = ctx.record(TABLE, schedule_id)
    d = ctx.record("xp_b_delivery_contracts", r["subject_id"])
    ctx.require(
        r["number"] == -34 and r.get("booking") and (not r.get("started")), "COMMISSIONING_STATE"
    )
    ctx.require(d.get("delivery_accepted"), "DELIVERY_NOT_ACCEPTED")
    ctx.schedule(
        "b_reasoning_commissioned", ctx.minute + r["services"][r["booking"]]["delay"], r["id"]
    )
    return upd(ctx, r, started=True, started_minute=ctx.minute)


@op(
    "reserve_store_dispatch_channel",
    "Reserve one dispatch instruction on a published channel. Capacity counts dispatched lines, and the booking fee is retained even if the chosen stock later proves unsuitable.",
    schedule_id=("string", "Original storefront delivery schedule."),
    channel=("string", "Channel key in dispatch services."),
)
def book_dispatch(ctx, schedule_id, channel):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(r["number"] == 34 and channel in r["channels"], "DISPATCH_SCOPE")
    ch = r["channels"][channel]
    used = sum((b["channel"] == channel for b in ctx.records(BOOK) if b["schedule_id"] == r["id"]))
    ctx.require(
        ctx.minute <= ch["book_until"] and used < ch["instructions"], "DISPATCH_BOOKING_LIMIT"
    )
    charge(ctx, r, ch["fee_minor"], "store_dispatch_booking")
    return ctx.create(
        BOOK, schedule_id=r["id"], channel=channel, status="RESERVED", minute=ctx.minute
    )


@op(
    "dispatch_store_delivery_line",
    "Irrevocably dispatch specific physical units for one paid order line through a booked channel. Stock is consumed once. The warehouse accepts the selected units; contractual grade and destination deadline remain the merchant responsibility.",
    schedule_id=("string", "Storefront delivery schedule."),
    line_id=("string", "Delivery line ID."),
    stock_ids=("array", "Distinct original or substitute physical stock IDs."),
    booking_id=("string", "Unused dispatch booking."),
)
def dispatch_line(ctx, schedule_id, line_id, stock_ids, booking_id):
    r = ctx.record(TABLE, schedule_id)
    line = ctx.record(LINES, line_id)
    b = ctx.record(BOOK, booking_id)
    f = ctx.record("xp_b_order_fulfillments", r["subject_id"])
    order = ctx.record("xp_b_store_orders", f["store_order_id"])
    ctx.require(
        r["number"] == 34 and line["schedule_id"] == r["id"] and (b["schedule_id"] == r["id"]),
        "DISPATCH_SCOPE",
    )
    ctx.require(
        order["status"] == "paid" and (not line.get("dispatched")) and (b["status"] == "RESERVED"),
        "DISPATCH_STATE",
    )
    ctx.require(len(set(stock_ids)) == len(stock_ids) == line["quantity"], "DISPATCH_QUANTITY")
    stock = [ctx.record(STOCK, i) for i in stock_ids]
    ctx.require(
        all((x["schedule_id"] == r["id"] and (not x.get("used")) for x in stock)),
        "STOCK_ALREADY_USED",
    )
    for x in stock:
        ctx.update(STOCK, x["id"], used=True, line_id=line_id)
    due = ctx.minute + r["channels"][b["channel"]]["delay"]
    ctx.update(BOOK, b["id"], status="USED", line_id=line_id)
    ctx.update(
        LINES,
        line_id,
        dispatched=True,
        stock_ids=stock_ids,
        due_minute=due,
        dispatched_minute=ctx.minute,
    )
    ctx.schedule("b_reasoning_store_line", due, line_id)
    lines = [x for x in ctx.records(LINES) if x["schedule_id"] == r["id"]]
    if all((x.get("dispatched") for x in lines)):
        ctx.update(
            "xp_b_order_fulfillments",
            f["id"],
            dispatched=True,
            dispatch_units=sum((x["quantity"] for x in lines)),
            paid_receipt_ids=deepcopy(order["receipt_payment_ids"]),
        )
        ctx.schedule(
            "b_enhance_delivery",
            max((x["due_minute"] for x in lines)),
            f["id"],
            table="xp_b_order_fulfillments",
            fields={
                "delivered_units": sum((x["quantity"] for x in lines)),
                "delivery_complete": True,
            },
        )
    return ctx.record(LINES, line_id)


@op(
    "review_platform_return_liability",
    "Request the final customer-return amount attached to accepted platform revenue. The published range is a forecast; the review event establishes the actual remaining obligation.",
    schedule_id=("string", "Platform revenue settlement calendar."),
)
def review_returns(ctx, schedule_id):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(r["number"] == 35 and (not r.get("return_review_requested")), "RETURN_REVIEW_STATE")
    ctx.schedule("b_reasoning_platform_returns", ctx.minute + r["return_review_delay"], r["id"])
    return upd(ctx, r, return_review_requested=True)


@op(
    "reserve_platform_refund_channel",
    "Reserve the pre-booked refund channel at its published nonrefundable fee. This reserves one execution, not the customer refund amount.",
    schedule_id=("string", "Platform revenue settlement calendar."),
    channel=("string", "Refund channel key."),
)
def reserve_refund(ctx, schedule_id, channel):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(
        r["number"] == 35 and channel in r["refund_channels"] and (not r.get("refund_booking")),
        "REFUND_RESERVATION_STATE",
    )
    ch = r["refund_channels"][channel]
    ctx.require(
        ch.get("book_until") is not None and ctx.minute <= ch["book_until"], "REFUND_BOOKING_CLOSED"
    )
    charge(ctx, r, ch["fee_minor"], "platform_refund_booking")
    return upd(ctx, r, refund_booking=channel)


@op(
    "pay_platform_customer_return",
    "Pay the reviewed customer refund from actual available company cash. The selected channel creates actual recipient receipt after its stated delay.",
    schedule_id=("string", "Platform settlement schedule."),
    channel=("string", "Refund channel key."),
    amount_minor=("integer", "Positive customer refund principal in minor units."),
)
def pay_return(ctx, schedule_id, channel, amount_minor):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(
        r["number"] == 35
        and r.get("return_reviewed")
        and (not r.get("refund_order"))
        and (channel in r["refund_channels"])
        and (amount_minor > 0),
        "RETURN_PAYMENT_STATE",
    )
    ch = r["refund_channels"][channel]
    if ch.get("book_until") is not None:
        ctx.require(r.get("refund_booking") == channel, "REFUND_CHANNEL_NOT_RESERVED")
    else:
        charge(ctx, r, ch["fee_minor"], "platform_refund_execution_fee")
    charge(ctx, r, amount_minor, "platform_customer_refund")
    x = ctx.create(
        TRANSFERS,
        schedule_id=r["id"],
        kind="CUSTOMER_REFUND",
        amount_minor=amount_minor,
        status="PENDING",
        recipient_id=r["return_recipient_id"],
        minute=ctx.minute,
        due_minute=ctx.minute + ch["delay"],
    )
    upd(ctx, r, refund_order=x["id"])
    ctx.schedule("b_reasoning_platform_transfer", x["due_minute"], x["id"])
    return x


@op(
    "sweep_platform_receipts",
    "Transfer positive available platform proceeds to the company treasury using a listed service. Early transfers are irrevocable; the bank does not reserve cash for unpaid customer-return obligations.",
    schedule_id=("string", "Platform settlement schedule."),
    service=("string", "Treasury sweep service key."),
    amount_minor=("integer", "Positive treasury principal in minor units."),
)
def sweep(ctx, schedule_id, service, amount_minor):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(
        r["number"] == 35 and service in r["sweep_services"] and (amount_minor > 0),
        "SWEEP_ARGUMENTS",
    )
    s = r["sweep_services"][service]
    ctx.require(ctx.minute >= s["from_minute"], "TREASURY_SERVICE_NOT_OPEN")
    charge(ctx, r, s["fee_minor"], "platform_sweep_fee")
    charge(ctx, r, amount_minor, "platform_treasury_sweep")
    x = ctx.create(
        TRANSFERS,
        schedule_id=r["id"],
        kind="TREASURY",
        amount_minor=amount_minor,
        status="PENDING",
        recipient_id=r["treasury_recipient_id"],
        minute=ctx.minute,
        due_minute=ctx.minute + s["delay"],
    )
    ctx.schedule("b_reasoning_platform_transfer", x["due_minute"], x["id"])
    return x


def register(operations, old_event):
    operations.update(OPS)
    old_sub = operations["commit_store_order_substitute"]["function"]

    def substitute(ctx, **kw):
        result = old_sub(ctx, **kw)
        r = p(ctx, 34)
        if r and r["subject_id"] == kw["record_id"]:
            f = ctx.record("xp_b_order_fulfillments", r["subject_id"])
            s = f["substitutes"][kw["choice"]]
            for index in range(f["substitute_units"]):
                ctx.create(
                    STOCK,
                    schedule_id=r["id"],
                    origin="SUBSTITUTE",
                    socket=s["socket"],
                    rating=s["rating"],
                    used=False,
                    unit_index=index,
                )
        return result

    operations["commit_store_order_substitute"]["function"] = substitute
    old_manual = operations["settle_manual_platform_receipt"]["function"]

    def manual(ctx, **kw):
        r = p(ctx, 35)
        if not r or r["subject_id"] != kw["record_id"]:
            return old_manual(ctx, **kw)
        f = ctx.record("xp_b_platform_reconciliations", r["subject_id"])
        receiving = ctx.record("xp_b_receiving_accounts", f["receiving_id"])
        ctx.require(
            receiving["status"] == "active"
            and receiving["manual_supported"]
            and (not r.get("manual_requested")),
            "MANUAL_RECEIPT_STATE",
        )
        upd(ctx, r, manual_requested=True)
        ctx.schedule("b_reasoning_manual_receipt", ctx.minute + r["manual_delay"], r["id"])
        return {"status": "PENDING", "due_minute": ctx.minute + r["manual_delay"]}

    operations["settle_manual_platform_receipt"]["function"] = manual

    def event(ctx, e):
        k = e["kind"]
        if k == "b_reasoning_commissioned":
            r = ctx.record(TABLE, e["subject_id"])
            return upd(ctx, r, commissioned=True, commissioned_minute=ctx.minute)
        if k == "b_reasoning_store_line":
            return ctx.update(LINES, e["subject_id"], delivered=True, delivered_minute=ctx.minute)
        if k == "b_reasoning_platform_returns":
            r = ctx.record(TABLE, e["subject_id"])
            return upd(ctx, r, return_reviewed=True, return_amount_minor=r["_return_amount_minor"])
        if k == "b_reasoning_manual_receipt":
            r = ctx.record(TABLE, e["subject_id"])
            f = ctx.record("xp_b_platform_reconciliations", r["subject_id"])
            ctx.ledger(
                r["account_id"],
                f["manual_receipt_minor"],
                "USD",
                "manual_platform_receipt",
                f["id"],
            )
            return ctx.update(
                "xp_b_platform_reconciliations",
                f["id"],
                manual_received=True,
                manual_reference=f["manual_reference"],
            )
        if k == "b_reasoning_platform_transfer":
            return ctx.update(
                TRANSFERS, e["subject_id"], status="RECEIVED", received_minute=ctx.minute
            )
        result = old_event(ctx, e)
        if k == "b_enhance_inventory":
            r = p(ctx, 34)
            if r and e["subject_id"] == r["subject_id"] and (not r.get("stock_published")):
                f = ctx.record("xp_b_order_fulfillments", r["subject_id"])
                for index, rating in enumerate(r["_stock_ratings"][: f["available_units"]]):
                    ctx.create(
                        STOCK,
                        schedule_id=r["id"],
                        origin="ORIGINAL",
                        socket=f["required_socket"],
                        rating=rating,
                        used=False,
                        unit_index=index,
                    )
                upd(ctx, r, stock_published=True)
        return result

    return event
