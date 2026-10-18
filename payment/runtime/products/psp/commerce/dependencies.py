"""Opt-in commercial couplings. Absence of a scoped source record preserves v1."""

from copy import deepcopy


def scoped(ctx, table, source_id=None):
    rows = [
        r
        for r in ctx.records(table)
        if r.get("reasoning_version") == "c-reasoning-v2"
        and (source_id is None or r.get("source_id") == source_id)
    ]
    return rows[0] if rows else None


def register(api):
    op = api.helper("operation")
    old_event = api.event_handler

    def debit(ctx, aid, amount, kind, ref):
        return ctx.ledger(aid, -amount, ctx.record("xp_accounts", aid)["currency"], kind, ref)

    def wrap(name, fn):
        original = api.operations[name]["function"]
        api.operations[name]["function"] = lambda ctx, _orig=original, _fn=fn, **args: _fn(
            ctx, _orig, args
        )

    def confirmation(ctx, original, args):
        r = scoped(ctx, "xp_c_supplier_portfolios", args["change_id"])
        if r is None:
            return original(ctx, **args)
        receipt = original(ctx, **args)
        if args["channel"] == "email_reply":
            return receipt
        account = receipt.pop("authorized_account")
        receipt.update(independent=False, status="PENDING")
        ctx.schedule(
            "c_reasoning_supplier_confirmation",
            ctx.minute + r["channel_delay_minutes"][args["channel"]],
            receipt["id"],
            authorized_account=account,
            portfolio_id=r["id"],
            discount_confirmed=r["_discount_confirmed"],
        )
        return receipt

    wrap("verify_supplier_change", confirmation)

    @op(
        "pay_supplier_due_item",
        "Pay one additional supplier invoice. amount_minor is its contractual net due: the early discount requires an independent completed confirmation, the named account, and both invoices paid by discount_until_minute. Public transfer fees are additional.",
        {
            "item_id": "string",
            "account_reference": "string",
            "amount_minor": "integer",
            "speed": ["standard", "instant"],
        },
    )
    def supplier_due(ctx, item_id, account_reference, amount_minor, speed):
        i = ctx.record("xp_c_supplier_due_items", item_id)
        r = ctx.record("xp_c_supplier_portfolios", i["portfolio_id"])
        ctx.require(r.get("reasoning_version") == "c-reasoning-v2", "PORTFOLIO_NOT_ENABLED")
        source = ctx.record("xp_c_supplier_changes", r["source_id"])
        ctx.require(i["status"] == "OPEN", "INVOICE_ALREADY_PAID")
        ctx.require(account_reference in source["bank_valid_accounts"], "INVALID_BANK_ACCOUNT")
        eligible = (
            source["status"] == "PAID"
            and source.get("paid_minute", 10**9) <= i["discount_until_minute"]
            and (ctx.minute <= i["discount_until_minute"])
            and r.get("discount_confirmed", False)
            and (account_reference in i["discount_accounts"])
        )
        net = i["face_minor"] - (i["discount_minor"] if eligible else 0)
        ctx.require(amount_minor == net, "INVOICE_NET_AMOUNT_MISMATCH")
        fee = i["transfer_fees_minor"][speed]
        debit(ctx, r["account_id"], net, "supplier_additional_principal", i["id"])
        debit(ctx, r["account_id"], fee, "supplier_additional_fee", i["id"])
        i.update(
            status="PAID",
            paid_minor=net,
            discount_used_minor=i["face_minor"] - net,
            paid_account=account_reference,
            paid_minute=ctx.minute,
            fee_minor=fee,
        )
        return i

    def exchange(ctx, original, args):
        b = ctx.record("xp_c_cash_bags", args["bag_id"])
        r = scoped(ctx, "xp_c_vault_allocations", b.get("intake_transaction_id"))
        if r is None:
            return original(ctx, **args)
        ctx.require(r.get("float_notice_received"), "CASH_FLOAT_NOTICE_PENDING")
        amount = sum((int(k) * v for (k, v) in b["second_counts"].items()))
        den = args["denomination_minor"]
        stock = r["note_stock"].get(str(den), 0)
        ctx.require(
            den > 0 and amount % den == 0 and (stock >= amount // den),
            "SHARED_VAULT_DENOMINATION_UNAVAILABLE",
        )
        prior = dict(b["second_counts"])
        result = original(ctx, **args)
        r["note_stock"][str(den)] -= amount // den
        for k, v in prior.items():
            r["note_stock"][k] = r["note_stock"].get(k, 0) + v
        ctx.emit(
            "shared_vault_exchange",
            allocation_id=r["id"],
            bag_id=b["id"],
            returned_notes=prior,
            issued_notes=dict(b["second_counts"]),
        )
        return result

    wrap("exchange_recovery_notes", exchange)

    @op(
        "request_staff_float_notice",
        "Request the independent cash-room confirmation of the already-funded staff float. The notice supplies its exact value, collector and note restrictions; it does not create another withdrawal.",
        {"allocation_id": "string"},
    )
    def float_notice(ctx, allocation_id):
        r = ctx.record("xp_c_vault_allocations", allocation_id)
        ctx.require(
            r.get("reasoning_version") == "c-reasoning-v2"
            and (not r.get("float_notice_requested")),
            "NOTICE_NOT_AVAILABLE",
        )
        r["float_notice_requested"] = True
        ctx.schedule(
            "c_reasoning_float_notice",
            ctx.minute + 1,
            r["id"],
            amount_minor=r["_float_minor"],
            max_note_minor=r["_float_max_note_minor"],
            max_notes=r["_float_max_notes"],
        )
        return r

    @op(
        "dispense_staff_float",
        "Deliver the funded staff float from the same vault denomination inventory used by recovery exchanges. note_counts maps denomination minor units to a positive integer count; customer preferences may be narrower than bank-permitted collectors.",
        {"allocation_id": "string", "collector_id": "string", "note_counts": "object"},
    )
    def dispense(ctx, allocation_id, collector_id, note_counts):
        r = ctx.record("xp_c_vault_allocations", allocation_id)
        ctx.require(
            r.get("reasoning_version") == "c-reasoning-v2"
            and r.get("float_notice_received")
            and (not r.get("float_delivered")),
            "FUNDED_FLOAT_NOT_AVAILABLE",
        )
        collector = ctx.record("xp_c_cash_collectors", collector_id)
        ctx.require(
            collector["active"] and collector["identity_checked"], "COLLECTOR_NOT_AVAILABLE"
        )
        ctx.require(
            bool(note_counts)
            and all(
                (
                    k in r["note_stock"]
                    and type(v) is int
                    and (v > 0)
                    and (r["note_stock"][k] >= v)
                    for (k, v) in note_counts.items()
                )
            ),
            "DENOMINATION_STOCK_UNAVAILABLE",
        )
        amount = sum((int(k) * v for (k, v) in note_counts.items()))
        ctx.require(amount == r["float_minor"], "FLOAT_AMOUNT_MISMATCH")
        for k, v in note_counts.items():
            r["note_stock"][k] -= v
        debit(ctx, r["vault_reserve_account"], amount, "staff_float_cash_handover", r["id"])
        receipt = ctx.create(
            "xp_c_staff_float_receipts",
            allocation_id=r["id"],
            collector_id=collector_id,
            collector_name=collector["name"],
            amount_minor=amount,
            note_counts=note_counts,
            minute=ctx.minute,
        )
        r.update(float_delivered=True, float_receipt_id=receipt["id"])
        return receipt

    api.operations["dispense_staff_float"]["parameters"]["properties"]["note_counts"] = {
        "type": "object",
        "additionalProperties": {"type": "integer", "minimum": 1},
    }

    def standing_advance(ctx, original, args):
        result = original(ctx, **args)
        if args.get("action") != "record_returned_payable":
            return result
        candidates = ctx.records("xp_c_supplier_advances")
        for r in candidates:
            if (
                r.get("reasoning_version") != "c-reasoning-v2"
                or r.get("standing_applied")
                or r.get("binding_id") != args.get("record_id")
            ):
                continue
            matches = ctx.records(
                "xp_c_restored_supplier_payables", execution_id=r["return_execution"]
            )
            if not matches:
                continue
            p = matches[0]
            amount = min(r["standing_application_minor"], r["available_minor"], p["open_minor"])
            r["standing_applied"] = True
            if amount:
                r["available_minor"] -= amount
                p["open_minor"] -= amount
                ctx.create(
                    "xp_c_advance_applications",
                    advance_id=r["id"],
                    payable_id=p["id"],
                    amount_minor=amount,
                    status="APPLIED",
                    minute=ctx.minute,
                    source="standing_supplier_instruction",
                )
        return result

    wrap("manage_recovery_period_binding", standing_advance)

    @op(
        "request_supplier_calloff_notice",
        "Obtain the supplier notice for the next call-off delivery supported by an existing advance. The bank publishes the actual required units and their existing advance price; this does not pay new cash.",
        {"advance_id": "string"},
    )
    def calloff_notice(ctx, advance_id):
        r = ctx.record("xp_c_supplier_advances", advance_id)
        ctx.require(
            r.get("reasoning_version") == "c-reasoning-v2" and (not r.get("notice_requested")),
            "NOTICE_NOT_AVAILABLE",
        )
        r["notice_requested"] = True
        ctx.schedule(
            "c_reasoning_calloff_notice", ctx.minute + 1, r["id"], units=r["_required_units"]
        )
        return r

    @op(
        "apply_supplier_advance",
        "Apply amount_minor of the supplier advance to the named restored payable. It consumes an existing asset right rather than bank cash. The separate call-off commitment may still need some of that same right.",
        {"advance_id": "string", "payable_id": "string", "amount_minor": "integer"},
    )
    def apply_advance(ctx, advance_id, payable_id, amount_minor):
        r = ctx.record("xp_c_supplier_advances", advance_id)
        p = ctx.record("xp_c_restored_supplier_payables", payable_id)
        ctx.require(
            r.get("reasoning_version") == "c-reasoning-v2"
            and amount_minor > 0
            and (amount_minor <= r["available_minor"]),
            "ADVANCE_UNAVAILABLE",
        )
        ctx.require(
            p["execution_id"] == r["return_execution"] and amount_minor <= p["open_minor"],
            "PAYABLE_AMOUNT_MISMATCH",
        )
        r["available_minor"] -= amount_minor
        p["open_minor"] -= amount_minor
        return ctx.create(
            "xp_c_advance_applications",
            advance_id=r["id"],
            payable_id=p["id"],
            amount_minor=amount_minor,
            status="APPLIED",
            minute=ctx.minute,
        )

    @op(
        "reverse_supplier_advance",
        "Reverse an unfinalized advance application and restore both its supplier asset right and outstanding payable. Applications remain reversible until a supplier call-off delivery is released.",
        {"application_id": "string"},
    )
    def reverse_advance(ctx, application_id):
        a = ctx.record("xp_c_advance_applications", application_id)
        r = ctx.record("xp_c_supplier_advances", a["advance_id"])
        p = ctx.record("xp_c_restored_supplier_payables", a["payable_id"])
        ctx.require(
            a["status"] == "APPLIED" and (not r.get("released_units")),
            "APPLICATION_NO_LONGER_REVERSIBLE",
        )
        a["status"] = "REVERSED"
        r["available_minor"] += a["amount_minor"]
        p["open_minor"] += a["amount_minor"]
        return a

    @op(
        "release_supplier_calloff",
        "Use units of existing supplier advance to release actual next-delivery units at unit_price_minor. Partial releases are permitted and final for those units; unreleased advance remains an asset.",
        {"advance_id": "string", "units": "integer"},
    )
    def release_calloff(ctx, advance_id, units):
        r = ctx.record("xp_c_supplier_advances", advance_id)
        ctx.require(
            r.get("reasoning_version") == "c-reasoning-v2" and r.get("notice_received"),
            "SUPPLIER_NOTICE_REQUIRED",
        )
        ctx.require(
            units > 0 and units <= r["required_units"] - r.get("released_units", 0),
            "INVALID_DELIVERY_QUANTITY",
        )
        amount = units * r["unit_price_minor"]
        ctx.require(amount <= r["available_minor"], "ADVANCE_INSUFFICIENT_FOR_DELIVERY")
        r["available_minor"] -= amount
        r["released_units"] = r.get("released_units", 0) + units
        return ctx.create(
            "xp_c_calloff_deliveries",
            advance_id=r["id"],
            units=units,
            advance_used_minor=amount,
            minute=ctx.minute,
        )

    def event(ctx, e):
        p = e.get("_payload", {})
        if e["kind"] == "c_reasoning_supplier_confirmation":
            receipt = ctx.record("xp_c_contact_receipts", e["subject_id"])
            receipt.update(
                independent=True,
                authorized_account=p["authorized_account"],
                status="COMPLETE",
                completed_minute=ctx.minute,
            )
            r = ctx.record("xp_c_supplier_portfolios", p["portfolio_id"])
            r.update(discount_confirmed=p["discount_confirmed"], confirmation_minute=ctx.minute)
            return receipt
        if e["kind"] == "c_reasoning_float_notice":
            return ctx.update(
                "xp_c_vault_allocations",
                e["subject_id"],
                float_notice_received=True,
                float_minor=p["amount_minor"],
                float_max_note_minor=p["max_note_minor"],
                float_max_notes=p["max_notes"],
            )
        if e["kind"] == "c_reasoning_calloff_notice":
            return ctx.update(
                "xp_c_supplier_advances",
                e["subject_id"],
                notice_received=True,
                required_units=p["units"],
            )
        return old_event(ctx, e)

    api.event_handler = event
