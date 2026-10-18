"""Cash custody and minimum-outflow business operations."""


def register(api):
    op = api.helper("operation")
    old_event = api.event_handler
    for key in (
        "retrieve_period_correction",
        "post_period_adjustment",
        "close_current_period",
        "manage_correction_components",
        "manage_provisional_cash_claim",
    ):
        api.operations.pop(key, None)

    def plan(ctx):
        return ctx.records("xp_c_exit_plans")[0]

    def wallet(ctx, wid):
        r = ctx.record("xp_c_exit_wallets", wid)
        ctx.require(r["status"] == "OPEN", "WALLET_CLOSED")
        return r

    def cash(ctx, aid):
        return ctx.record("xp_accounts", aid)

    def move(ctx, aid, amount, kind, ref):
        ctx.ledger(aid, amount, cash(ctx, aid)["currency"], kind, ref)

    @op(
        "request_wallet_closure_quote",
        "Request the final closure charge assessment for wallet_id; the scheduled notice supplies the actual fee.",
        {"wallet_id": "string"},
    )
    def quote(ctx, wallet_id):
        r = wallet(ctx, wallet_id)
        ctx.require(r["retiring"] and r["fee_status"] == "UNASSESSED", "FEE_REVIEW_NOT_AVAILABLE")
        ctx.update("xp_c_exit_wallets", r["id"], fee_status="PENDING")
        ctx.schedule("c_dust_fee", ctx.minute + 1, r["id"], fee_minor=r["_closure_fee_minor"])
        return r

    @op(
        "settle_wallet_closure_charge",
        "Pay the assessed closure charge from its own wallet; fee_minor is read from the bank assessment.",
        {"wallet_id": "string"},
    )
    def closure_fee(ctx, wallet_id):
        r = wallet(ctx, wallet_id)
        ctx.require(r["fee_status"] == "ASSESSED", "FINAL_CLOSURE_FEE_REQUIRED")
        move(ctx, r["account_id"], -r["fee_minor"], "dust_closure_fee", r["id"])
        return ctx.update("xp_c_exit_wallets", r["id"], fee_status="PAID")

    @op(
        "consolidate_exit_cash",
        "Transfer positive amount_minor between same-currency customer wallets. This does not change their ownership or close either wallet.",
        {
            "source_wallet_id": "string",
            "destination_wallet_id": "string",
            "amount_minor": "integer",
        },
    )
    def consolidate(ctx, source_wallet_id, destination_wallet_id, amount_minor):
        s = wallet(ctx, source_wallet_id)
        d = wallet(ctx, destination_wallet_id)
        ctx.require(
            s["id"] != d["id"] and s["currency"] == d["currency"] and (amount_minor > 0),
            "INCOMPATIBLE_CONSOLIDATION",
        )
        move(ctx, s["account_id"], -amount_minor, "dust_consolidation_out", s["id"])
        move(ctx, d["account_id"], amount_minor, "dust_consolidation_in", s["id"])
        ctx.emit(
            "dust_cash_consolidated",
            source_wallet_id=s["id"],
            destination_wallet_id=d["id"],
            amount_minor=amount_minor,
        )
        return d

    @op(
        "request_small_outflow_permission",
        "Request currency-scoped small-outflow review; the public non-refundable USD fee is paid from fee_wallet_id. Approval, cap and expiry arrive in a bank event.",
        {"currency": "string", "fee_wallet_id": "string"},
    )
    def permission(ctx, currency, fee_wallet_id):
        p = plan(ctx)
        w = wallet(ctx, fee_wallet_id)
        ctx.require(w["currency"] == "USD", "USD_FEE_WALLET_REQUIRED")
        ctx.require(currency in ("USD", "EUR"), "UNKNOWN_CURRENCY")
        move(ctx, w["account_id"], -p["permission_fee_minor"], "dust_permission_fee", p["id"])
        r = ctx.create(
            "xp_c_small_outflow_permissions",
            currency=currency,
            status="PENDING",
            application_fee_minor=p["permission_fee_minor"],
        )
        ctx.schedule(
            "c_dust_permission",
            ctx.minute + p["permission_review_minutes"],
            r["id"],
            approved=currency in p["_approved_currencies"],
            maximum_net_minor=p["permission_maximum_net_minor"],
            lifetime_minutes=p["permission_lifetime_minutes"],
        )
        return r

    @op(
        "convert_exit_balance",
        "Convert source_amount_minor EUR from source_wallet_id into a USD destination_wallet_id at the public fixed rate, rounding down before deducting the public USD conversion fee.",
        {
            "source_wallet_id": "string",
            "destination_wallet_id": "string",
            "source_amount_minor": "integer",
        },
    )
    def convert(ctx, source_wallet_id, destination_wallet_id, source_amount_minor):
        s = wallet(ctx, source_wallet_id)
        d = wallet(ctx, destination_wallet_id)
        p = plan(ctx)
        ctx.require(
            s["currency"] == "EUR" and d["currency"] == "USD" and (source_amount_minor > 0),
            "UNSUPPORTED_CONVERSION",
        )
        gross = source_amount_minor * p["eur_usd_numerator"] // p["eur_usd_denominator"]
        net = gross - p["conversion_fee_minor"]
        ctx.require(net > 0, "CONVERSION_TOO_SMALL")
        move(ctx, s["account_id"], -source_amount_minor, "dust_conversion_out", p["id"])
        move(ctx, d["account_id"], gross, "dust_conversion_in", p["id"])
        move(ctx, d["account_id"], -p["conversion_fee_minor"], "dust_conversion_fee", p["id"])
        return ctx.emit(
            "dust_converted",
            source_wallet_id=s["id"],
            destination_wallet_id=d["id"],
            source_amount_minor=source_amount_minor,
            net_destination_minor=net,
        )

    @op(
        "submit_exit_outflow",
        "Debit gross_amount_minor, subtract the selected tariff fee and deliver the net to external_account_id. The destination-currency net minimum or a current permission_id must be satisfied. Use an empty permission_id when relying on the normal minimum.",
        {
            "wallet_id": "string",
            "external_account_id": "string",
            "gross_amount_minor": "integer",
            "tariff_id": "string",
            "permission_id": "string",
        },
    )
    def payout(ctx, wallet_id, external_account_id, gross_amount_minor, tariff_id, permission_id):
        w = wallet(ctx, wallet_id)
        t = ctx.record("xp_c_outflow_tariffs", tariff_id)
        d = cash(ctx, external_account_id)
        p = plan(ctx)
        ctx.require(
            d.get("external") and d["currency"] == w["currency"] == t["currency"],
            "DESTINATION_CURRENCY_MISMATCH",
        )
        ctx.require(ctx.minute <= p["deadline_minute"], "PROCESSING_WINDOW_CLOSED")
        fee = t["fee_minor"]
        net = gross_amount_minor - fee
        ctx.require(net > 0, "OUTFLOW_NET_NOT_POSITIVE")
        if net < t["minimum_net_minor"]:
            permit = ctx.record("xp_c_small_outflow_permissions", permission_id)
            ctx.require(
                permit["status"] == "APPROVED"
                and permit["currency"] == w["currency"]
                and (ctx.minute <= permit["expires_minute"])
                and (net <= permit["maximum_net_minor"]),
                "NO_CURRENT_SMALL_OUTFLOW_PERMISSION",
            )
        move(ctx, w["account_id"], -net, "dust_outflow_principal", w["id"])
        move(ctx, w["account_id"], -fee, "dust_outflow_fee", w["id"])
        r = ctx.create(
            "xp_c_exit_transfers",
            wallet_id=w["id"],
            external_account_id=d["id"],
            currency=w["currency"],
            gross_minor=gross_amount_minor,
            net_minor=net,
            fee_minor=fee,
            status="PENDING",
            submitted_minute=ctx.minute,
            tariff_id=t["id"],
            permission_id=permission_id,
        )
        ctx.schedule("c_dust_delivery", ctx.minute + 1, r["id"])
        return r

    @op(
        "close_retiring_wallet",
        "Close one retiring wallet after its balance is zero, final incoming settlement and closure charge are complete, and every submitted outflow has settled.",
        {"wallet_id": "string"},
    )
    def close(ctx, wallet_id):
        w = wallet(ctx, wallet_id)
        ctx.require(
            w["retiring"] and cash(ctx, w["account_id"])["balance_minor"] == 0,
            "WALLET_MUST_BE_EMPTY",
        )
        ctx.require(
            w["fee_status"] == "PAID" and w["incoming_status"] == "COMPLETE",
            "OPEN_WALLET_OBLIGATIONS",
        )
        ctx.require(
            all(
                (
                    t["status"] == "SETTLED"
                    for t in ctx.records("xp_c_exit_transfers", wallet_id=w["id"])
                )
            ),
            "OUTFLOW_SETTLEMENT_PENDING",
        )
        ctx.update("xp_c_exit_wallets", w["id"], status="CLOSED", closed_minute=ctx.minute)
        ctx.emit("dust_wallet_closed", wallet_id=w["id"])
        return w

    def transaction(ctx, tid):
        return ctx.record("xp_c_cash_transactions", tid)

    def bag(ctx, bid):
        return ctx.record("xp_c_cash_bags", bid)

    @op(
        "request_cash_intake",
        "Ask the vault to publish the sealed-bag intake for cash_transaction_id; a scheduled bank event supplies original references and the first independent count.",
        {"cash_transaction_id": "string"},
    )
    def intake(ctx, cash_transaction_id):
        t = transaction(ctx, cash_transaction_id)
        ctx.require(not t.get("intake_requested"), "INTAKE_ALREADY_REQUESTED")
        t["intake_requested"] = True
        ctx.schedule("c_cash_intake", ctx.minute + 1, t["id"])
        return t

    @op(
        "count_cash_bag",
        "Obtain the independent denomination count of a published vault bag using counter_name from its licensed_counters. It must differ from the first intake counter.",
        {"bag_id": "string", "counter_name": "string"},
    )
    def count(ctx, bag_id, counter_name):
        b = bag(ctx, bag_id)
        ctx.require(b["status"] == "INTAKED", "VAULT_INTAKE_REQUIRED")
        ctx.require(
            counter_name in b["licensed_counters"] and counter_name != b["first_counter"],
            "INDEPENDENT_COUNTER_REQUIRED",
        )
        b.update(
            second_counter=counter_name, second_counts=dict(b["_second_counts"]), status="COUNTED"
        )
        b["counts_agree"] = b["first_counts"] == b["second_counts"]
        ctx.emit(
            "cash_bag_counted",
            bag_id=b["id"],
            original_withdrawal_reference=b["withdrawal_reference"],
            first_counter=b["first_counter"],
            second_counter=counter_name,
            first_counts=b["first_counts"],
            second_counts=b["second_counts"],
        )
        return b

    @op(
        "allocate_recovery_bag",
        "Associate a counted customer bag with an open cash withdrawal by customer attestation. The immutable original withdrawal reference remains visible; this action cannot change that source fact.",
        {"bag_id": "string", "cash_transaction_id": "string"},
    )
    def allocate(ctx, bag_id, cash_transaction_id):
        b = bag(ctx, bag_id)
        t = transaction(ctx, cash_transaction_id)
        ctx.require(
            b["status"] == "COUNTED" and b["counts_agree"] and (t["status"] == "OPEN"),
            "OPEN_COUNTED_RECOVERY_REQUIRED",
        )
        b["allocated_transaction_id"] = t["id"]
        return b

    @op(
        "exchange_recovery_notes",
        "Replace all notes in a counted bag with equal-value denomination_minor notes. Available denominations and quantities are public in denomination_exchange; the vault supplies a new agreeing dual count.",
        {"bag_id": "string", "denomination_minor": "integer"},
    )
    def exchange(ctx, bag_id, denomination_minor):
        b = bag(ctx, bag_id)
        ctx.require(b["status"] == "COUNTED" and b["counts_agree"], "COUNTED_BAG_REQUIRED")
        amount = sum((int(k) * v for (k, v) in b["second_counts"].items()))
        stock = b["denomination_exchange"].get(str(denomination_minor), 0)
        ctx.require(
            denomination_minor > 0
            and amount % denomination_minor == 0
            and (stock >= amount // denomination_minor),
            "NOTE_EXCHANGE_UNAVAILABLE",
        )
        qty = amount // denomination_minor
        b["denomination_exchange"][str(denomination_minor)] -= qty
        prior = dict(b["second_counts"])
        for denomination, count in prior.items():
            b["denomination_exchange"][denomination] = (
                b["denomination_exchange"].get(denomination, 0) + count
            )
        b.update(
            first_counts={str(denomination_minor): qty},
            second_counts={str(denomination_minor): qty},
        )
        ctx.emit(
            "cash_notes_exchanged",
            bag_id=b["id"],
            before_counts=prior,
            after_counts=b["second_counts"],
            amount_minor=amount,
        )
        return b

    @op(
        "cash_handover",
        "Hand the whole counted and allocated bag to an active identity-checked collector_id. This physically removes notes from vault custody and consumes that withdrawal recovery right; the recipient and original bag reference are immutable.",
        {"bag_id": "string", "collector_id": "string"},
    )
    def handover(ctx, bag_id, collector_id):
        b = bag(ctx, bag_id)
        collector = ctx.record("xp_c_cash_collectors", collector_id)
        ctx.require(
            b["status"] == "COUNTED" and b["counts_agree"] and b.get("allocated_transaction_id"),
            "COUNTED_ALLOCATED_BAG_REQUIRED",
        )
        t = transaction(ctx, b["allocated_transaction_id"])
        ctx.require(
            t["status"] == "OPEN" and t.get("log_received"), "OPEN_EVIDENCED_CLAIM_REQUIRED"
        )
        amount = sum((int(k) * v for (k, v) in b["second_counts"].items()))
        shortage = t["debited_minor"] - t["presented_minor"] + t["retracted_minor"]
        ctx.require(
            collector["active"] and collector["identity_checked"], "COLLECTOR_NOT_AVAILABLE"
        )
        ctx.require(
            amount <= shortage - t.get("physical_recovered_minor", 0),
            "BAG_EXCEEDS_REMAINING_RECOVERY",
        )
        move(ctx, b["vault_account_id"], -amount, "cash_physical_handover", b["id"])
        t["physical_recovered_minor"] = t.get("physical_recovered_minor", 0) + amount
        b.update(status="DELIVERED", collector_id=collector["id"])
        return ctx.create(
            "xp_c_cash_handovers",
            bag_id=b["id"],
            cash_transaction_id=t["id"],
            original_withdrawal_reference=b["withdrawal_reference"],
            amount_minor=amount,
            note_counts=dict(b["second_counts"]),
            collector_id=collector["id"],
            collector_name=collector["name"],
            minute=ctx.minute,
        )

    @op(
        "settle_cash_shortfall",
        "Close a synthetic withdrawal claim. exact credits evidenced shortage minus physical cash already handed over. full_and_final credits the public offer capped at that remaining right and waives the rest; waive credits zero. Physical recovery must precede this final disposition.",
        {
            "cash_transaction_id": "string",
            "choice": ["exact", "full_and_final", "waive"],
            "amount_minor": "integer",
        },
    )
    def settle(ctx, cash_transaction_id, choice, amount_minor):
        t = transaction(ctx, cash_transaction_id)
        ctx.require(t["status"] == "OPEN", "CLAIM_ALREADY_CLOSED")
        ctx.require(t.get("log_received"), "CASH_DELIVERY_LOG_REQUIRED")
        remaining = (
            t["debited_minor"]
            - t["presented_minor"]
            + t["retracted_minor"]
            - t.get("physical_recovered_minor", 0)
        )
        expected = (
            remaining
            if choice == "exact"
            else min(t["settlement_offer_minor"], remaining) if choice == "full_and_final" else 0
        )
        ctx.require(amount_minor == expected, "CASH_CORRECTION_AMOUNT_MISMATCH")
        move(ctx, t["account_id"], expected, "cash_shortfall_credit", t["id"])
        t.update(
            status="CLOSED",
            credited_minor=expected,
            settlement_choice=choice,
            waived_minor=remaining - expected,
        )
        ctx.emit(
            "cash_claim_settled",
            cash_transaction_id=t["id"],
            bank_credit_minor=expected,
            physical_minor=t.get("physical_recovered_minor", 0),
            waived_minor=remaining - expected,
        )
        return t

    def event(ctx, e):
        kind = e["kind"]
        p = e.get("_payload", {})
        if kind == "c_dust_incoming":
            w = ctx.record("xp_c_exit_wallets", e["subject_id"])
            move(ctx, w["account_id"], p["amount_minor"], "dust_final_settlement", w["id"])
            w.update(incoming_status="COMPLETE", settlement_received_minor=p["amount_minor"])
            return w
        if kind == "c_dust_fee":
            return ctx.update(
                "xp_c_exit_wallets",
                e["subject_id"],
                fee_status="ASSESSED",
                fee_minor=p["fee_minor"],
            )
        if kind == "c_dust_permission":
            result = ctx.update(
                "xp_c_small_outflow_permissions",
                e["subject_id"],
                status="APPROVED" if p["approved"] else "DECLINED",
                maximum_net_minor=p["maximum_net_minor"],
                expires_minute=ctx.minute + p["lifetime_minutes"],
            )
            if p["approved"]:
                ctx.schedule(
                    "c_dust_permission_expired",
                    ctx.minute + p["lifetime_minutes"] + 1,
                    e["subject_id"],
                )
            return result
        if kind == "c_dust_permission_expired":
            return ctx.update("xp_c_small_outflow_permissions", e["subject_id"], status="EXPIRED")
        if kind == "c_dust_delivery":
            t = ctx.record("xp_c_exit_transfers", e["subject_id"])
            move(ctx, t["external_account_id"], t["net_minor"], "dust_external_credit", t["id"])
            t.update(status="SETTLED", settled_minute=ctx.minute)
            return t
        if kind == "c_cash_intake":
            t = transaction(ctx, e["subject_id"])
            for b in ctx.records("xp_c_cash_bags", intake_transaction_id=t["id"]):
                b.update(
                    status="INTAKED",
                    withdrawal_reference=b["_withdrawal_reference"],
                    first_counts=dict(b["_first_counts"]),
                    first_counter=b["licensed_counters"][0],
                )
            t["intake_received"] = True
            return t
        return old_event(ctx, e)

    api.event_handler = event
