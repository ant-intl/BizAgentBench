"""Opt-in capacity, asynchronous currency collateral and priced routing products."""

from payment.runtime.products.psp.commerce.dependencies import scoped


def register(api):
    op = api.helper("operation")
    old_event = api.event_handler

    def wrap(name, fn):
        old = api.operations[name]["function"]
        api.operations[name]["function"] = lambda ctx, _old=old, _fn=fn, **kw: _fn(ctx, _old, kw)

    def cash(ctx, account, amount, kind, ref):
        return ctx.ledger(
            account, amount, ctx.record("xp_accounts", account)["currency"], kind, ref
        )

    @op(
        "reserve_fallback_capacity",
        "Reserve amount_minor of currently free standard-fallback principal for one batch item; the posted reservation fee is nonrefundable. A zero amount releases its unused reservation without a fee refund.",
        {"capacity_id": "string", "item_id": "string", "amount_minor": "integer"},
    )
    def reserve(ctx, capacity_id, item_id, amount_minor):
        r = ctx.record("xp_c_fallback_capacity", capacity_id)
        item = ctx.record("xp_c_channel_items", item_id)
        ctx.require(
            item["batch_id"] == r["source_id"] and amount_minor >= 0, "CAPACITY_SCOPE_MISMATCH"
        )
        current = r["protected"].get(item_id, 0)
        if amount_minor > current:
            delta = amount_minor - current
            ctx.require(delta <= r["free_minor"], "FALLBACK_QUOTA_UNAVAILABLE")
            cash(
                ctx,
                r["account_id"],
                -r["reservation_fee_minor"],
                "fallback_reservation_fee",
                r["id"],
            )
        r["free_minor"] += current - amount_minor
        r["protected"][item_id] = amount_minor
        ctx.emit(
            "fallback_capacity_reserved",
            capacity_id=r["id"],
            item_id=item_id,
            amount_minor=amount_minor,
        )
        return r

    def replacement(ctx, old, k):
        item = ctx.record("xp_c_channel_items", k["item_id"])
        r = scoped(ctx, "xp_c_fallback_capacity", item["batch_id"])
        if not r:
            return old(ctx, **k)
        if k["route"] == "standard_fallback":
            protected = r["protected"].get(item["id"], 0)
            used = min(protected, item["amount_minor"])
            free = item["amount_minor"] - used
            ctx.require(free <= r["free_minor"], "FALLBACK_QUOTA_UNAVAILABLE")
            r["free_minor"] -= free
            r["protected"][item["id"]] = protected - used
        result = old(ctx, **k)
        if result.get("item_id"):
            result["delivered_minute"] = ctx.minute
            if not r.get("notice_requested"):
                r["notice_requested"] = True
                ctx.schedule("c_reasoning_network_capacity", ctx.minute + 1, r["id"])
        return result

    wrap("send_channel_replacement", replacement)

    def refresh(ctx, old, k):
        r = scoped(ctx, "xp_c_fallback_capacity", k["batch_id"])
        if not r:
            return old(ctx, **k)
        batch = ctx.record("xp_c_channel_batches", k["batch_id"])
        batch["recovery_available"] = bool(
            r.get("notice_received") and ctx.minute >= r["recovery_minute"]
        )
        before = {x["id"] for x in ctx.records("xp_c_channel_deliveries")}
        result = old(ctx, **k)
        for d in ctx.records("xp_c_channel_deliveries"):
            if d["id"] not in before:
                d["delivered_minute"] = ctx.minute
        return result

    wrap("refresh_channel_settlements", refresh)

    @op(
        "draw_currency_release_bridge",
        "Advance same-currency principal against the still-active deferred reservation after its first release notice. amount_minor cannot exceed remaining collateral. The posted fee is paid from coordination cash; loan principal must later be repaid.",
        {"release_id": "string", "amount_minor": "integer"},
    )
    def bridge(ctx, release_id, amount_minor):
        r = ctx.record("xp_c_deferred_currency_releases", release_id)
        hold = ctx.record("xp_c_reservations", r["hold_id"])
        ctx.require(
            r.get("first_notice")
            and (not r.get("loan_minor"))
            and (0 < amount_minor <= hold["amount_minor"])
            and hold["active"],
            "BRIDGE_COLLATERAL_UNAVAILABLE",
        )
        cash(ctx, r["account_id"], -r["bridge_fee_minor"], "currency_bridge_fee", r["id"])
        cash(ctx, hold["funding_account_id"], amount_minor, "currency_bridge_advance", r["id"])
        r["loan_minor"] = amount_minor
        return r

    @op(
        "repay_currency_release_bridge",
        "Repay the outstanding bridge from unreserved cash in its original currency after releases make that cash available. Full principal is required.",
        {"release_id": "string"},
    )
    def repay(ctx, release_id):
        r = ctx.record("xp_c_deferred_currency_releases", release_id)
        hold = ctx.record("xp_c_reservations", r["hold_id"])
        amount = r.get("loan_minor", 0)
        unavailable = sum(
            (
                h["amount_minor"]
                for h in ctx.records(
                    "xp_c_reservations", funding_account_id=hold["funding_account_id"]
                )
                if h["active"]
            )
        )
        ctx.require(
            amount > 0
            and ctx.record("xp_accounts", hold["funding_account_id"])["balance_minor"] - unavailable
            >= amount,
            "UNRESERVED_REPAYMENT_CASH_REQUIRED",
        )
        cash(ctx, hold["funding_account_id"], -amount, "currency_bridge_repayment", r["id"])
        r["repaid_minor"] = r.get("repaid_minor", 0) + amount
        r["loan_minor"] = 0
        return r

    def execute(ctx, old, k):
        p = ctx.record("xp_c_resource_payments", k["payment_id"])
        r = scoped(ctx, "xp_c_deferred_currency_releases", p["queue_id"])
        if not r:
            return old(ctx, **k)
        h = ctx.record("xp_c_reservations", r["hold_id"])
        defer = h["active"] and h["owner_id"] == p["id"] and (h["disposition"] == "OLD_PLAN")
        amount = h["amount_minor"]
        result = old(ctx, **k)
        result["paid_minute"] = ctx.minute
        if defer:
            h.update(active=True, amount_minor=amount, disposition="RELEASE_PENDING")
            r["initiated_minute"] = ctx.minute
            r["original_hold_minor"] = amount
            ctx.schedule(
                "c_reasoning_currency_first_release", ctx.minute + r["first_delay"], r["id"]
            )
            ctx.schedule(
                "c_reasoning_currency_final_release", ctx.minute + r["final_delay"], r["id"]
            )
        return result

    wrap("execute_reserved_payment", execute)

    def route(ctx, old, k):
        r = scoped(ctx, "xp_c_route_portfolios", k["offer_id"])
        if not r:
            return old(ctx, **k)
        offer = ctx.record("xp_c_route_offers", k["offer_id"])
        if k["route"] == "economical":
            ctx.require(
                offer["principal_minor"] <= r["remaining_minor"], "ECONOMICAL_QUOTA_UNAVAILABLE"
            )
            r["remaining_minor"] -= offer["principal_minor"]
        result = old(ctx, **k)
        external = min(r["remaining_minor"], r["_external_quota_minor"])
        r["remaining_minor"] -= external
        r.update(
            notice_received=True,
            external_quota_minor=external,
            correspondent_fee_minor=r["_second_correspondent_fee_minor"],
            notice_minute=ctx.minute,
        )
        ctx.emit(
            "route_portfolio_capacity_notice",
            portfolio_id=r["id"],
            remaining_minor=r["remaining_minor"],
            external_quota_minor=external,
            correspondent_fee_minor=r["correspondent_fee_minor"],
        )
        return result

    wrap("commit_payment_route", route)

    @op(
        "remit_second_supplier_installment",
        "Pay amount_minor toward the enrolled second supplier using economical, conservative or premium. Economical consumes shared certified quota; conservative has the public per-ticket ceiling and fee formula. fee_bearer is SENDER or SHARED, and correspondent cost applies only to the first installment. Bank permits excess remittance; it does not amend the supplier invoice.",
        {
            "portfolio_id": "string",
            "amount_minor": "integer",
            "route": ["economical", "conservative", "premium"],
            "fee_bearer": ["SENDER", "SHARED"],
        },
    )
    def installment(ctx, portfolio_id, amount_minor, route, fee_bearer):
        r = ctx.record("xp_c_route_portfolios", portfolio_id)
        o = ctx.record("xp_c_route_offers", r["source_id"])
        ctx.require(
            r.get("notice_received") and amount_minor > 0, "FIRST_SUPPLIER_RECEIPT_REQUIRED"
        )
        if route == "economical":
            ctx.require(o["eligibility"] == "ELIGIBLE", "FORMAL_ELIGIBILITY_REQUIRED")
            ctx.require(amount_minor <= r["remaining_minor"], "ECONOMICAL_QUOTA_UNAVAILABLE")
            fee = r["economical_fee_minor"]
            r["remaining_minor"] -= amount_minor
        elif route == "conservative":
            ctx.require(amount_minor <= r["ticket_cap_minor"], "CLEARING_TICKET_CEILING")
            fee = r["clearing_fixed_fee_minor"] + amount_minor * r["clearing_fee_bps"] // 10000
        else:
            fee = r["premium_fee_minor"]
        correspondent = 0 if r.get("installments") else r["correspondent_fee_minor"]
        net = amount_minor - (correspondent if fee_bearer == "SHARED" else 0)
        cash(ctx, r["account_id"], -amount_minor, "second_supplier_principal", r["id"])
        cash(ctx, r["account_id"], -fee, "second_supplier_route_fee", r["id"])
        if fee_bearer == "SENDER":
            cash(ctx, r["account_id"], -correspondent, "second_supplier_correspondent_fee", r["id"])
        receipt = ctx.create(
            "xp_c_supplier_installments",
            customer_id=r["customer_id"],
            portfolio_id=r["id"],
            beneficiary=r["second_beneficiary"],
            amount_minor=amount_minor,
            received_minor=net,
            route=route,
            fee_minor=fee,
            correspondent_fee_minor=correspondent,
            fee_bearer=fee_bearer,
            paid_minute=ctx.minute,
        )
        r["installments"] = r.get("installments", 0) + 1
        return receipt

    def event(ctx, e):
        if e["kind"] == "c_reasoning_network_capacity":
            r = ctx.record("xp_c_fallback_capacity", e["subject_id"])
            before = r["free_minor"]
            r.update(
                free_minor=max(0, before - r["_external_reserved_minor"]),
                notice_received=True,
                recovery_minute=r["_recovery_minute"],
            )
            ctx.schedule("c_reasoning_channel_recovery_ready", r["recovery_minute"], r["id"])
            return r
        if e["kind"] in (
            "c_reasoning_channel_recovery_ready",
            "c_reasoning_channel_deadline_notice",
        ):
            return {"available": True}
        if e["kind"].startswith("c_reasoning_currency_"):
            r = ctx.record("xp_c_deferred_currency_releases", e["subject_id"])
            h = ctx.record("xp_c_reservations", r["hold_id"])
            if e["kind"] == "c_reasoning_currency_first_release":
                released = min(h["amount_minor"], r["_first_release_minor"])
                h["amount_minor"] -= released
                h["active"] = h["amount_minor"] > 0
                r.update(
                    first_notice=True, released_first_minor=released, first_notice_minute=ctx.minute
                )
            else:
                h.update(active=False, amount_minor=0, disposition="RELEASE_CONFIRMED")
                r["final_notice_minute"] = ctx.minute
            ctx.emit(
                "currency_reservation_released",
                release_id=r["id"],
                hold_id=h["id"],
                remaining_minor=h["amount_minor"],
            )
            return r
        return old_event(ctx, e)

    api.event_handler = event
