"""Opt-in A reasoning revision. Existing contracts without a matching profile
retain the original operations, events and money semantics.
"""

from copy import deepcopy

OPS = {}
BASE = {}
S = "string"
I = "integer"
A = "array"


def operation(name, description, **fields):

    def decorate(fn):
        props = {
            k: dict(type=t, description=d, **{"items": {"type": "string"}} if t == A else {})
            for (k, (t, d)) in fields.items()
        }
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=props, required=list(props), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return decorate


def rows(ctx, t):
    return ctx.records("xp_a_" + t)


def row(ctx, t, i):
    return ctx.record("xp_a_" + t, i)


def update(ctx, t, i, **kw):
    return ctx.update("xp_a_" + t, i, **kw)


def profile(ctx, number=None, subject_id=None):
    return next(
        (
            r
            for r in rows(ctx, "reasoning_contracts")
            if (number is None or r["number"] == number)
            and (subject_id is None or subject_id in r["subject_ids"])
        ),
        None,
    )


def need(ctx, contract_id, number=None):
    r = row(ctx, "reasoning_contracts", contract_id)
    ctx.require(number is None or r["number"] == number, "PRODUCT_SCOPE")
    return r


def ledger(ctx, r, amount, kind, reference=None):
    if amount:
        ctx.ledger(r["account_id"], amount, "USD", kind, reference or r["id"])


@operation(
    "a_request_carrier_allotment",
    "Request the actual carrier allotment for this existing purchase. Published timetable entries are provisional until carriers return their commercial capacity and departure window. This does not buy goods or reserve transport.",
    contract_id=(S, "Purchase delivery contract ID."),
)
def carrier_review(ctx, contract_id):
    r = need(ctx, contract_id)
    ctx.require(r["number"] in (-21, -33), "PRODUCT_SCOPE")
    ctx.require(not r.get("review_requested"), "ALLOTMENT_ALREADY_REQUESTED")
    update(ctx, "reasoning_contracts", r["id"], review_requested=True)
    ctx.schedule("a_reasoning_carrier_allotment", ctx.minute + r["review_delay_minutes"], r["id"])
    return {"status": "REQUESTED", "contract_id": r["id"]}


@operation(
    "a_reserve_goods_dispatch",
    "Reserve published carrier capacity for the listed paid-or-planned goods. One item can have multiple units; a grouped order list carries one unit of each item. A booking does not buy those goods. A booking may be cancelled before dispatch; the capacity then returns to the same carrier.",
    contract_id=(S, "Purchase delivery contract ID."),
    service_id=(S, "Confirmed carrier service ID."),
    subject_ids=(A, "Original order IDs or the stock invoice ID."),
    quantity=(I, "Total original units carried by this booking."),
)
def reserve_dispatch(ctx, contract_id, service_id, subject_ids, quantity):
    r = need(ctx, contract_id)
    s = row(ctx, "reasoning_carriers", service_id)
    ctx.require(
        s["contract_id"] == r["id"]
        and (r.get("review_complete") or s.get("conditional_reservations")),
        "CARRIER_NOT_CONFIRMED",
    )
    ctx.require(
        subject_ids
        and len(subject_ids) == len(set(subject_ids))
        and (set(subject_ids) <= set(r["subject_ids"])),
        "ORIGINAL_GOODS_SCOPE",
    )
    ctx.require(
        quantity > 0 and (len(subject_ids) == 1 or quantity == len(subject_ids)),
        "DISPATCH_QUANTITY",
    )
    ctx.require(
        ctx.minute <= s["book_until"] and quantity <= s["capacity_remaining"],
        "CARRIER_CAPACITY_OR_WINDOW",
    )
    reservation = ctx.create(
        "xp_a_reasoning_dispatches",
        customer_id=r["customer_id"],
        contract_id=r["id"],
        service_id=s["id"],
        subject_ids=subject_ids,
        quantity=quantity,
        requested_quantity=quantity,
        lines={i: quantity if len(subject_ids) == 1 else 1 for i in subject_ids},
        status="RESERVED",
        booked_minute=ctx.minute,
    )
    ledger(ctx, r, -s.get("booking_fee_minor", 0), "REASONING_CARRIER_BOOKING", reservation["id"])
    update(
        ctx, "reasoning_carriers", s["id"], capacity_remaining=s["capacity_remaining"] - quantity
    )
    return reservation


@operation(
    "a_cancel_goods_dispatch",
    "Cancel an unexecuted carrier booking, returning its actual reserved capacity. Delivered goods and already charged carriage cannot be recalled through this operation.",
    reservation_id=(S, "Unexecuted carrier booking ID."),
)
def cancel_dispatch(ctx, reservation_id):
    reservation = row(ctx, "reasoning_dispatches", reservation_id)
    ctx.require(reservation["status"] == "RESERVED", "DISPATCH_ALREADY_EXECUTED")
    s = row(ctx, "reasoning_carriers", reservation["service_id"])
    update(
        ctx,
        "reasoning_carriers",
        s["id"],
        capacity_remaining=s["capacity_remaining"] + reservation["quantity"],
    )
    return update(ctx, "reasoning_dispatches", reservation_id, status="CANCELLED")


@operation(
    "a_dispatch_purchased_goods",
    "Dispatch actual paid goods under existing carrier bookings. Charges each selected booking fee once and schedules physical delivery from its confirmed transit time. Merchant-funded promotions can lose qualification when their original goods use a different fulfilment provider; lost value is repaid as real cash.",
    contract_id=(S, "Original purchase delivery contract ID."),
    reservation_ids=(A, "Carrier bookings to execute now."),
)
def dispatch(ctx, contract_id, reservation_ids):
    r = need(ctx, contract_id)
    ctx.require(
        reservation_ids and len(reservation_ids) == len(set(reservation_ids)), "DISTINCT_DISPATCHES"
    )
    bookings = [row(ctx, "reasoning_dispatches", i) for i in reservation_ids]
    ctx.require(
        all((b["contract_id"] == r["id"] and b["status"] == "RESERVED" for b in bookings)),
        "DISPATCH_BOOKING_STATE",
    )
    for b in bookings:
        s = row(ctx, "reasoning_carriers", b["service_id"])
        if r["number"] == -21:
            paid = any(
                (
                    p["invoice_id"] in b["subject_ids"] and p["status"] == "SETTLED"
                    for p in rows(ctx, "payments")
                )
            )
            ctx.require(paid, "PURCHASE_NOT_PAID")
        else:
            qs = [
                q
                for q in rows(ctx, "basket_quotes")
                if q["status"] == "PAID" and set(q["order_ids"]) & set(b["subject_ids"])
            ]
            ctx.require(
                all((any((i in q["order_ids"] for q in qs)) for i in b["subject_ids"])),
                "PURCHASE_NOT_PAID",
            )
            ctx.require(
                all((q["method"] in s["checkout_methods"] for q in qs)), "CARRIER_CHECKOUT_METHOD"
            )
            for q in qs:
                already = set(r.get("repaid_benefit_ids", []))
                for bid in filter(None, q.get("benefit_ids", [q["benefit_id"]])):
                    benefit = row(ctx, "benefits", bid)
                    if (
                        s["provider_family"]
                        not in benefit.get(
                            "fulfilment_families", ["MERCHANT", "INDEPENDENT", "STORE"]
                        )
                        and bid not in already
                    ):
                        ledger(
                            ctx,
                            r,
                            -benefit["discount_minor"],
                            "REASONING_PROMOTION_REPAYMENT",
                            q["id"],
                        )
                        already.add(bid)
                r = update(ctx, "reasoning_contracts", r["id"], repaid_benefit_ids=sorted(already))
        ledger(ctx, r, -s["fee_minor"], "REASONING_CARRIAGE_FEE", b["id"])
        due = ctx.minute + s["transit_minutes"]
        update(
            ctx,
            "reasoning_dispatches",
            b["id"],
            status="DISPATCHED",
            dispatched_minute=ctx.minute,
            due_minute=due,
            fee_paid_minor=s["fee_minor"],
        )
        ctx.schedule("a_reasoning_goods_arrival", due, b["id"])
    return {"dispatched_ids": reservation_ids, "status": "IN_TRANSIT"}


def register(api):
    api.operations.update(OPS)
    old_event = api.event_handler

    def event(ctx, e):
        if e["kind"] == "a_reasoning_carrier_allotment":
            r = need(ctx, e["subject_id"])
            for sid, fields in r["_carrier_results"].items():
                capacity = fields["capacity"]
                for booking in sorted(
                    (
                        b
                        for b in rows(ctx, "reasoning_dispatches")
                        if b["service_id"] == sid and b["status"] == "RESERVED"
                    ),
                    key=lambda b: b["id"],
                ):
                    retained = min(capacity, booking["quantity"])
                    capacity -= retained
                    retained_lines = {}
                    left = retained
                    for subject, quantity in booking["lines"].items():
                        use = min(left, quantity)
                        left -= use
                        if use:
                            retained_lines[subject] = use
                    update(
                        ctx,
                        "reasoning_dispatches",
                        booking["id"],
                        quantity=retained,
                        lines=retained_lines,
                        subject_ids=list(retained_lines),
                        status="RESERVED" if retained else "DECLINED",
                        carrier_confirmed_minute=ctx.minute,
                    )
                update(
                    ctx, "reasoning_carriers", sid, **deepcopy(fields), capacity_remaining=capacity
                )
            update(
                ctx,
                "reasoning_contracts",
                r["id"],
                review_complete=True,
                reviewed_minute=ctx.minute,
            )
            return True
        if e["kind"] == "a_reasoning_goods_arrival":
            b = row(ctx, "reasoning_dispatches", e["subject_id"])
            update(
                ctx,
                "reasoning_dispatches",
                b["id"],
                status="DELIVERED",
                delivered_minute=ctx.minute,
            )
            return True
        return old_event(ctx, e)

    api.event_handler = event
    old_refund = api.operations["a_claim_basket_price_adjustment"]["function"]

    def price_refund(ctx, **args):
        r = profile(ctx, -33)
        if not r:
            return old_refund(ctx, **args)
        q = row(ctx, "basket_quotes", args["quote_id"])
        disposition = args["disposition"]
        adjustment = next(
            (
                x
                for x in rows(ctx, "basket_adjustments")
                if x["source_id"] == row(ctx, "basket_orders", q["order_ids"][0])["terms_id"]
            )
        )
        ctx.require(
            q["status"] == "PAID"
            and adjustment.get("advice_received")
            and (not adjustment.get("disposed"))
            and (disposition in ("REFUND", "FUTURE")),
            "PRICE_ADJUSTMENT_STATE",
        )
        refund = adjustment["refund_minor"]
        clawback = 0
        paid = set(r.get("repaid_benefit_ids", []))
        if disposition == "REFUND":
            ledger(ctx, r, refund, "MERCHANT_PRICE_REFUND", adjustment["id"])
            for bid in filter(None, q.get("benefit_ids", [q["benefit_id"]])):
                benefit = row(ctx, "benefits", bid)
                if (
                    q["subtotal_minor"] - refund < benefit.get("retained_spend_floor_minor", 0)
                    and bid not in paid
                ):
                    clawback += benefit["discount_minor"]
                    paid.add(bid)
            ledger(ctx, r, -clawback, "PROMOTION_CLAWBACK", adjustment["id"])
            update(ctx, "reasoning_contracts", r["id"], repaid_benefit_ids=sorted(paid))
        return update(
            ctx,
            "basket_adjustments",
            adjustment["id"],
            disposed=True,
            disposition=disposition,
            refund_paid_minor=refund if disposition == "REFUND" else 0,
            clawback_minor=clawback,
            quote_id=q["id"],
        )

    api.operations["a_claim_basket_price_adjustment"]["function"] = price_refund

    from payment.runtime.products.psp.receivables.custody_and_credit import (
        register as install_settlement,
    )

    install_settlement(api)
    from payment.runtime.products.psp.receivables.filing_windows import register as install_windows

    install_windows(api)
    from payment.runtime.products.psp.receivables.mandates_and_rights import (
        register as install_rights,
    )

    install_rights(api)
