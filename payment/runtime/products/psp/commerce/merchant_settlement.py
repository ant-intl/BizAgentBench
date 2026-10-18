"""Scoped physical delivery, card authority and merchant settlement products."""

from payment.runtime.products.psp.commerce.dependencies import scoped


def register(api):
    op = api.helper("operation")
    old_event = api.event_handler

    def wrap(name, fn):
        old = api.operations[name]["function"]
        api.operations[name]["function"] = lambda ctx, _old=old, _fn=fn, **kw: _fn(ctx, _old, kw)

    def cash(ctx, r, amt, kind):
        return ctx.ledger(r["account_id"], amt, "USD", kind, r["id"])

    def replacement(ctx, old, k):
        r = scoped(ctx, "xp_c_freight_contracts")
        linked = ctx.record("xp_c_delivery_acceptances", k["record_id"])
        if (
            r is None
            or r["source_id"] != linked["source_id"]
            or k["action"] != "request_replacement"
        ):
            return old(ctx, **k)
        before = linked["received_quantity"]
        result = old(ctx, **k)
        linked["received_quantity"] = before
        linked.pop("replacement_reference", None)
        ctx.schedule(
            "c_reasoning_goods_arrival",
            ctx.minute + r["_replacement_delay"],
            r["id"],
            acceptance_id=linked["id"],
            quantity=linked["contract_quantity"],
        )
        r["replacement_pending"] = True
        return result

    wrap("manage_delivery_acceptance", replacement)

    @op(
        "book_screened_freight",
        "Book a real carrier departure for the screened supplier consignment. Its nonrefundable charge consumes the same payment account as the goods. Booking does not waive its pickup cutoff.",
        {"freight_id": "string", "route_id": "string"},
    )
    def book(ctx, freight_id, route_id):
        r = ctx.record("xp_c_freight_contracts", freight_id)
        route = ctx.record("xp_c_freight_routes", route_id)
        ctx.require(route["freight_id"] == r["id"], "WRONG_CONSIGNMENT")
        cash(ctx, r, -route["fee_minor"], "screened_freight_booking")
        return ctx.create(
            "xp_c_freight_bookings",
            freight_id=r["id"],
            route_id=route_id,
            fee_minor=route["fee_minor"],
            booked_minute=ctx.minute,
            status="BOOKED",
        )

    @op(
        "dispatch_screened_goods",
        "Dispatch paid and accepted goods on the selected booking, if its published collection window remains open. The resulting arrival time is fixed by that departure.",
        {"booking_id": "string"},
    )
    def dispatch(ctx, booking_id):
        b = ctx.record("xp_c_freight_bookings", booking_id)
        r = ctx.record("xp_c_freight_contracts", b["freight_id"])
        route = ctx.record("xp_c_freight_routes", b["route_id"])
        hit = ctx.record("xp_c_screening_hits", r["source_id"])
        a = ctx.record("xp_c_delivery_acceptances", r["acceptance_id"])
        ctx.require(
            b["status"] == "BOOKED"
            and hit["status"] == "PAID"
            and (a.get("accepted_quantity") == a["contract_quantity"]),
            "PAID_ACCEPTED_GOODS_REQUIRED",
        )
        ctx.require(ctx.minute <= route["pickup_until_minute"], "DEPARTURE_MISSED")
        b.update(
            status="DISPATCHED",
            quantity=a["accepted_quantity"],
            arrival_minute=route["arrival_minute"],
        )
        return b

    @op(
        "submit_card_goods_return",
        "Submit a real return against the selected delivered authorization. The notice confirms the returned value and releases only that much cycle occupancy, with a credit to its original account.",
        {"return_id": "string", "authorization_reference": "string", "amount_minor": "integer"},
    )
    def goods_return(ctx, return_id, authorization_reference, amount_minor):
        r = ctx.record("xp_c_card_returns", return_id)
        a = ctx.record("xp_c_card_authorizations", r["authorization_id"])
        ctx.require(
            not r.get("submitted")
            and authorization_reference == a.get("delivered_authorization_reference"),
            "RETURN_SOURCE_NOT_AVAILABLE",
        )
        ctx.require(0 < amount_minor <= a["delivered_hold_minor"], "RETURN_EXCEEDS_DELIVERED_GOODS")
        r.update(submitted=True, submitted_minor=amount_minor)
        ctx.schedule(
            "c_reasoning_card_return",
            ctx.minute + 2,
            r["id"],
            amount_minor=min(amount_minor, r["_accepted_limit_minor"]),
        )
        return r

    @op(
        "present_essential_capture",
        "Present or void an unpresented old-generation essential-service capture. Presentation starts a bank notice; void removes this capture but does not cancel the current merchant invoice.",
        {"capture_id": "string", "disposition": ["present", "void"]},
    )
    def present(ctx, capture_id, disposition):
        r = ctx.record("xp_c_essential_captures", capture_id)
        ctx.require(r["status"] == "AUTHORIZED", "CAPTURE_ALREADY_DISPOSED")
        if disposition == "void":
            r["status"] = "VOIDED"
        else:
            r["status"] = "PENDING"
            ctx.schedule(
                "c_reasoning_essential_capture", ctx.minute + 2, r["id"], status=r["_bank_status"]
            )
        return r

    @op(
        "reconcile_essential_capture",
        "Allocate the actual settled old capture to its own service invoice or retain it as a separate merchant advance. An invoice allocation reduces the subsequent current-generation invoice charge.",
        {"capture_id": "string", "treatment": ["invoice_offset", "separate_advance"]},
    )
    def reconcile_capture(ctx, capture_id, treatment):
        r = ctx.record("xp_c_essential_captures", capture_id)
        ctx.require(
            r["status"] == "SETTLED" and (not r.get("treatment")),
            "SETTLED_UNALLOCATED_CAPTURE_REQUIRED",
        )
        token = ctx.record("xp_c_network_tokens", r["token_id"])
        ctx.require(not token["billed"], "INVOICE_ALREADY_BILLED")
        r["treatment"] = treatment
        if treatment == "invoice_offset":
            token["amount_minor"] = r["invoice_face_minor"] - r["capture_minor"]
        return r

    @op(
        "request_hotel_checkout_notice",
        "Obtain the current legitimate checkout statement for the existing hotel deposit. This statement is distinct from the card terminal currency consent evidence.",
        {"deposit_id": "string"},
    )
    def checkout(ctx, deposit_id):
        r = ctx.record("xp_c_hotel_deposits", deposit_id)
        ctx.require(not r.get("notice_requested"), "NOTICE_ALREADY_REQUESTED")
        r["notice_requested"] = True
        ctx.schedule(
            "c_reasoning_hotel_checkout",
            ctx.minute + 1,
            r["id"],
            incidentals_minor=r["_incidentals_minor"],
        )
        return r

    @op(
        "settle_hotel_deposit",
        "Settle the legitimate checkout amount using bank cash or the existing deposit, and return the remaining deposit as a real cash credit. No conversion claim is included in this calculation.",
        {"deposit_id": "string", "method": ["cash", "deposit_offset"], "amount_minor": "integer"},
    )
    def hotel(ctx, deposit_id, method, amount_minor):
        r = ctx.record("xp_c_hotel_deposits", deposit_id)
        ctx.require(
            r.get("notice_received") and (not r.get("settled")), "CURRENT_UNSETTLED_BILL_REQUIRED"
        )
        ctx.require(amount_minor == r["incidentals_minor"], "CHECKOUT_AMOUNT_MISMATCH")
        offset = amount_minor if method == "deposit_offset" else 0
        ctx.require(offset <= r["deposit_minor"], "DEPOSIT_TOO_SMALL")
        if method == "cash":
            cash(ctx, r, -amount_minor, "hotel_legitimate_checkout")
        cash(ctx, r, r["deposit_minor"] - offset, "hotel_deposit_release")
        r.update(
            settled=True,
            method=method,
            settled_minor=amount_minor,
            offset_minor=offset,
            returned_minor=r["deposit_minor"] - offset,
        )
        return r

    @op(
        "pay_onward_hotel_booking",
        "Pay a selected onward-stay booking plan from the original card account. The public plan terms state its price and cancellation charge; recipient names identify the actual hotel.",
        {"deposit_id": "string", "plan_id": "string"},
    )
    def booking(ctx, deposit_id, plan_id):
        r = ctx.record("xp_c_hotel_deposits", deposit_id)
        p = ctx.record("xp_c_onward_booking_plans", plan_id)
        ctx.require(
            p["deposit_id"] == deposit_id and (not r.get("booking_paid")), "BOOKING_NOT_AVAILABLE"
        )
        cash(ctx, r, -p["price_minor"], "onward_hotel_booking")
        r.update(
            booking_paid=True,
            booking_plan=plan_id,
            booking_paid_minor=p["price_minor"],
            booking_minute=ctx.minute,
        )
        return r

    def event(ctx, e):
        p = e.get("_payload", {})
        kind = e["kind"]
        if kind == "c_reasoning_goods_arrival":
            r = ctx.record("xp_c_freight_contracts", e["subject_id"])
            r["replacement_pending"] = False
            r["goods_arrival_minute"] = ctx.minute
            return ctx.update(
                "xp_c_delivery_acceptances",
                p["acceptance_id"],
                received_quantity=p["quantity"],
                replacement_reference="REPLACEMENT-DELIVERY-NOTICE",
            )
        if kind == "c_reasoning_card_return":
            r = ctx.record("xp_c_card_returns", e["subject_id"])
            a = ctx.record("xp_c_card_authorizations", r["authorization_id"])
            amount = p["amount_minor"]
            a["delivered_hold_minor"] -= amount
            cash(ctx, r, amount, "card_supplier_return")
            r.update(credited_minor=amount, status="CREDITED")
            return r
        if kind == "c_reasoning_essential_capture":
            r = ctx.record("xp_c_essential_captures", e["subject_id"])
            r["status"] = p["status"]
            if p["status"] == "SETTLED":
                cash(ctx, r, -r["capture_minor"], "essential_old_capture")
            return r
        if kind == "c_reasoning_hotel_checkout":
            return ctx.update(
                "xp_c_hotel_deposits",
                e["subject_id"],
                notice_received=True,
                incidentals_minor=p["incidentals_minor"],
            )
        return old_event(ctx, e)

    api.event_handler = event
