"""Commercial offsets and cash timing joined to actual source ledgers."""

from payment.runtime.products.psp.commerce.dependencies import scoped


def register(api):
    op = api.helper("operation")
    old_event = api.event_handler

    def wrap(name, fn):
        old = api.operations[name]["function"]
        api.operations[name]["function"] = lambda ctx, _old=old, _fn=fn, **kw: _fn(ctx, _old, kw)

    def cash(ctx, r, amount, kind):
        return ctx.ledger(r["account_id"], amount, "USD", kind, r["id"])

    @op(
        "request_cod_return_notice",
        "Obtain the accepted return attached to the original buyer order; the carrier will publish actual refundable value.",
        {"return_id": "string"},
    )
    def cod_notice(ctx, return_id):
        r = ctx.record("xp_c_cod_returns", return_id)
        ctx.require(not r.get("requested"), "NOTICE_ALREADY_REQUESTED")
        r["requested"] = True
        ctx.schedule(
            "c_reasoning_cod_return", ctx.minute + 1, r["id"], amount_minor=r["_refund_minor"]
        )
        return r

    @op(
        "select_cod_return_channel",
        "Choose carrier net remittance or merchant cash refund for the accepted buyer return before carrier cash is remitted.",
        {"return_id": "string", "channel": ["net_remittance", "cash_refund"]},
    )
    def cod_channel(ctx, return_id, channel):
        r = ctx.record("xp_c_cod_returns", return_id)
        b = ctx.record("xp_c_cod_batches", r["source_id"])
        ctx.require(
            r.get("notice_received") and (not b.get("cash_remitted")),
            "RETURN_CHANNEL_NO_LONGER_EDITABLE",
        )
        ctx.require(
            channel != "net_remittance" or r["refund_minor"] >= r["net_offset_minimum_minor"],
            "CARRIER_OFFSET_BELOW_MINIMUM",
        )
        r["channel"] = channel
        return r

    def remit(ctx, old, k):
        result = old(ctx, **k)
        r = scoped(ctx, "xp_c_cod_returns", k["batch_id"])
        if r and k["action"] == "remit_cash" and (r.get("channel") == "net_remittance"):
            cash(ctx, r, -r["refund_minor"], "carrier_buyer_return_offset")
            ctx.create(
                "xp_c_cod_buyer_refunds",
                return_id=r["id"],
                order_id=r["order_id"],
                amount_minor=r["refund_minor"],
                channel="CARRIER_AGENT",
                minute=ctx.minute,
            )
        return result

    wrap("settle_carrier_collection", remit)

    @op(
        "send_cod_buyer_refund",
        "Send a direct merchant refund to a delivered buyer. The bank validates its prior collected limit; the caller must separately reconcile the original accepted return and any carrier offset.",
        {"return_id": "string", "order_id": "string", "amount_minor": "integer"},
    )
    def refund(ctx, return_id, order_id, amount_minor):
        r = ctx.record("xp_c_cod_returns", return_id)
        o = ctx.record("xp_c_cod_orders", order_id)
        ctx.require(
            o["batch_id"] == r["source_id"]
            and o["delivery_status"] == "DELIVERED"
            and (0 < amount_minor <= o["collected_from_buyer_minor"]),
            "BUYER_REFUND_LIMIT",
        )
        cash(ctx, r, -amount_minor, "cod_direct_buyer_refund")
        return ctx.create(
            "xp_c_cod_buyer_refunds",
            return_id=r["id"],
            order_id=order_id,
            amount_minor=amount_minor,
            channel="MERCHANT_CASH",
            minute=ctx.minute,
        )

    @op(
        "pay_cod_replenishment",
        "Pay the replenishment invoice from the same carrier-proceeds account. A valid partial amount reduces the still-open invoice but does not settle the rest.",
        {"return_id": "string", "amount_minor": "integer"},
    )
    def replenish(ctx, return_id, amount_minor):
        r = ctx.record("xp_c_cod_returns", return_id)
        ctx.require(
            0 < amount_minor <= r["replenishment_minor"] - r.get("replenishment_paid_minor", 0),
            "REPLENISHMENT_AMOUNT_INVALID",
        )
        cash(ctx, r, -amount_minor, "cod_replenishment")
        r["replenishment_paid_minor"] = r.get("replenishment_paid_minor", 0) + amount_minor
        return r

    @op(
        "refund_prior_tax_overcollection",
        "Refund a prior-period tax overcollection through the bank or card processor. Both routes return the entire entered amount; processor costs are separate. The authority review follows the actual refund.",
        {
            "refund_id": "string",
            "recipient": "string",
            "route": ["bank", "card_processor"],
            "amount_minor": "integer",
        },
    )
    def tax_refund(ctx, refund_id, recipient, route, amount_minor):
        r = ctx.record("xp_c_prior_tax_refunds", refund_id)
        ctx.require(
            not r.get("refunded")
            and recipient in r["eligible_recipients"]
            and (amount_minor == r["refund_minor"]),
            "PRIOR_REFUND_NOT_AVAILABLE",
        )
        cash(ctx, r, -amount_minor, "prior_tax_buyer_refund")
        cash(ctx, r, -r["route_fees_minor"][route], "prior_tax_refund_fee")
        r.update(refunded=True, recipient=recipient, route=route)
        ctx.schedule(
            "c_reasoning_prior_tax_credit",
            ctx.minute + 1,
            r["id"],
            amount_minor=amount_minor,
            jurisdiction=r["_authority_jurisdiction"],
        )
        return r

    @op(
        "allocate_generated_tax_credit",
        "Assign the independently certified tax refund credit to CURRENT remittance or preserve it as CARRY_FORWARD. Only the certified jurisdiction may use it.",
        {"refund_id": "string", "period": ["CURRENT", "CARRY_FORWARD"], "jurisdiction": "string"},
    )
    def allocate(ctx, refund_id, period, jurisdiction):
        r = ctx.record("xp_c_prior_tax_refunds", refund_id)
        ctx.require(
            r.get("credit_received") and (not r.get("credit_used_minor")),
            "CERTIFIED_UNUSED_CREDIT_REQUIRED",
        )
        r.update(credit_period=period, assigned_jurisdiction=jurisdiction)
        return r

    def remit_tax(ctx, old, k):
        r = scoped(ctx, "xp_c_prior_tax_refunds", k["tax_batch_id"])
        if r is None or r.get("credit_period") != "CURRENT":
            return old(ctx, **k)
        linked = next(
            (x for x in ctx.records("xp_c_tax_registrations") if x["source_id"] == r["source_id"])
        )
        ctx.require(
            r["assigned_jurisdiction"] == r["credit_jurisdiction"] == linked.get("registration"),
            "NEW_TAX_CREDIT_JURISDICTION_MISMATCH",
        )
        extra = r["credit_minor"] - r.get("credit_used_minor", 0)
        reserved = linked.get("credit_reserved_minor", 0)
        used = linked.get("credit_used", 0)
        linked["credit_reserved_minor"] = reserved + extra
        result = old(ctx, **k)
        total_used = linked.get("credit_used", 0) - used
        old_available = max(0, reserved - used)
        new_used = max(0, total_used - old_available)
        left_new = new_used
        for item in reversed(result["remittances"]):
            portion = min(left_new, item["credit_minor"])
            item["credit_minor"] -= portion
            item["generated_refund_credit_minor"] = portion
            item["generated_credit_source"] = r["id"]
            left_new -= portion
        linked["credit_reserved_minor"] = reserved
        linked["credit_used"] -= new_used
        r["credit_used_minor"] = r.get("credit_used_minor", 0) + new_used
        return result

    wrap("remit_order_sales_tax", remit_tax)

    @op(
        "submit_treasury_redemption",
        "Commit the encumbered deposit to standard or accelerated redemption. The selected fee and due date are final; the independent notice releases the actual held amount.",
        {"distribution_id": "string", "channel": ["standard", "accelerated"]},
    )
    def redeem(ctx, distribution_id, channel):
        r = ctx.record("xp_c_treasury_distributions", distribution_id)
        ctx.require(not r.get("redemption_requested"), "REDEMPTION_ALREADY_COMMITTED")
        fee = r["redemption_fees_minor"][channel]
        aid = r["redemption_account_id"]
        ctx.require(
            ctx.record("xp_accounts", aid)["balance_minor"] - r["hold_by_account"][aid] >= fee,
            "AVAILABLE_CASH_TOO_SMALL",
        )
        ctx.ledger(aid, -fee, "USD", "treasury_redemption_fee", r["id"])
        r.update(redemption_requested=True, redemption_channel=channel)
        ctx.schedule(
            "c_reasoning_treasury_redemption",
            ctx.minute + r["redemption_delays"][channel],
            r["id"],
            released_minor=r["_released_minor"],
        )
        return r

    @op(
        "pay_statement_supported_distribution",
        "Pay an installment of the owner distribution or supplier invoice using unencumbered cash. The owner amount derives from actual consolidated external income; supplier settlement can claim its public early discount or pay full face.",
        {
            "distribution_id": "string",
            "obligation": ["OWNER", "SUPPLIER"],
            "account_id": "string",
            "amount_minor": "integer",
            "supplier_terms": ["early_discount", "full_face"],
        },
    )
    def distribute(ctx, distribution_id, obligation, account_id, amount_minor, supplier_terms):
        r = ctx.record("xp_c_treasury_distributions", distribution_id)
        linked = ctx.record("xp_c_interaccount_matches", r["source_id"])
        ctx.require(linked.get("consolidated_published"), "CONSOLIDATED_SOURCE_REQUIRED")
        ctx.require(
            account_id in r["hold_by_account"] and amount_minor > 0, "DISTRIBUTION_ACCOUNT_INVALID"
        )
        ctx.require(
            ctx.record("xp_accounts", account_id)["balance_minor"]
            - r["hold_by_account"][account_id]
            >= amount_minor,
            "CASH_STILL_ENCUMBERED",
        )
        if obligation == "OWNER":
            due = (
                linked["external_income_minor"]
                * r["distribution_numerator"]
                // r["distribution_denominator"]
            )
        else:
            if supplier_terms == "early_discount":
                ctx.require(ctx.minute <= r["discount_until_minute"], "SUPPLIER_DISCOUNT_EXPIRED")
            due = r["supplier_face_minor"] - (
                r["supplier_discount_minor"] if supplier_terms == "early_discount" else 0
            )
        prior = sum(
            (
                x["amount_minor"]
                for x in ctx.records(
                    "xp_c_treasury_disbursements", distribution_id=r["id"], obligation=obligation
                )
            )
        )
        ctx.require(prior + amount_minor <= due, "DISTRIBUTION_EXCEEDS_SELECTED_OBLIGATION")
        ctx.ledger(account_id, -amount_minor, "USD", "statement_supported_distribution", r["id"])
        return ctx.create(
            "xp_c_treasury_disbursements",
            distribution_id=r["id"],
            obligation=obligation,
            account_id=account_id,
            amount_minor=amount_minor,
            minute=ctx.minute,
            supplier_terms=supplier_terms,
        )

    def event(ctx, e):
        p = e.get("_payload", {})
        if e["kind"] == "c_reasoning_cod_return":
            return ctx.update(
                "xp_c_cod_returns",
                e["subject_id"],
                notice_received=True,
                refund_minor=p["amount_minor"],
            )
        if e["kind"] == "c_reasoning_prior_tax_credit":
            return ctx.update(
                "xp_c_prior_tax_refunds",
                e["subject_id"],
                credit_received=True,
                credit_minor=p["amount_minor"],
                credit_jurisdiction=p["jurisdiction"],
            )
        if e["kind"] == "c_reasoning_treasury_redemption":
            r = ctx.record("xp_c_treasury_distributions", e["subject_id"])
            r["hold_by_account"][r["redemption_account_id"]] -= p["released_minor"]
            r.update(released_minor=p["released_minor"], redemption_complete=True)
            return r
        return old_event(ctx, e)

    api.event_handler = event
