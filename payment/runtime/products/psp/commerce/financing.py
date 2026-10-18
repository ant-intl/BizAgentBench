"""Current-customer liquidation, institutional cash, debt tranches and payroll."""

from payment.runtime.products.psp.commerce.dependencies import scoped


def register(api):
    op = api.helper("operation")
    old_event = api.event_handler

    def wrap(name, fn):
        old = api.operations[name]["function"]
        api.operations[name]["function"] = lambda ctx, _old=old, _fn=fn, **kw: _fn(ctx, _old, kw)

    def cash(ctx, aid, amt, kind, r):
        return ctx.ledger(aid, amt, ctx.record("xp_accounts", aid)["currency"], kind, r["id"])

    @op(
        "request_retiring_merchant_notice",
        "Retrieve the final amount of an existing merchant authorization tied to the retiring wallets. This creates no new order.",
        {"claim_id": "string"},
    )
    def exit_notice(ctx, claim_id):
        r = ctx.record("xp_c_exit_merchant_claims", claim_id)
        ctx.require(not r.get("requested"), "NOTICE_ALREADY_REQUESTED")
        r["requested"] = True
        ctx.schedule(
            "c_reasoning_exit_claim", ctx.minute + 2, r["id"], amount_minor=r["_actual_eur_minor"]
        )
        return r

    @op(
        "settle_retiring_merchant",
        "Pay the actual retiring-wallet merchant bill, plus any explicitly chosen gratuity, in an accepted currency. USD payment uses its published conversion and collection fee.",
        {"claim_id": "string", "currency": ["EUR", "USD"], "merchant_amount_eur_minor": "integer"},
    )
    def exit_pay(ctx, claim_id, currency, merchant_amount_eur_minor):
        r = ctx.record("xp_c_exit_merchant_claims", claim_id)
        ctx.require(r.get("notice_received") and (not r.get("paid")), "OPEN_MERCHANT_BILL_REQUIRED")
        ctx.require(
            merchant_amount_eur_minor >= r["actual_eur_minor"] and merchant_amount_eur_minor > 0,
            "BELOW_MERCHANT_BILL",
        )
        aid = r["eur_account_id"] if currency == "EUR" else r["usd_account_id"]
        principal = (
            merchant_amount_eur_minor
            if currency == "EUR"
            else merchant_amount_eur_minor * r["rate_numerator"] // r["rate_denominator"]
        )
        fee = 0 if currency == "EUR" else r["usd_collection_fee_minor"]
        cash(ctx, aid, -principal, "retiring_merchant_principal", r)
        cash(ctx, aid, -fee, "retiring_merchant_fee", r)
        r.update(
            paid=True,
            paid_eur_minor=merchant_amount_eur_minor,
            currency=currency,
            principal_minor=principal,
            fee_minor=fee,
            paid_minute=ctx.minute,
        )
        return r

    def close(ctx, old, k):
        wallet = ctx.record("xp_c_exit_wallets", k["wallet_id"])
        r = scoped(ctx, "xp_c_exit_merchant_claims")
        if r and wallet["retiring"] and r["settlement_required"]:
            ctx.require(
                r.get("notice_received") and (r.get("paid") or not r.get("actual_eur_minor")),
                "MERCHANT_OBLIGATION_OPEN",
            )
        return old(ctx, **k)

    wrap("close_retiring_wallet", close)

    @op(
        "finance_institution_receivable",
        "Assign the institutional receivable without recourse, draw its offered credit line, or collect it at maturity. Assignment consumes the receivable; a credit line preserves it and creates real repayable principal.",
        {"finance_id": "string", "method": ["nonrecourse_assignment", "credit_line", "collection"]},
    )
    def finance(ctx, finance_id, method):
        r = ctx.record("xp_c_institution_finance", finance_id)
        if method == "collection":
            ctx.require(
                ctx.minute >= r["receivable_due_minute"]
                and (not r.get("assigned"))
                and (not r.get("collected")),
                "RECEIVABLE_NOT_COLLECTIBLE",
            )
            cash(
                ctx, r["account_id"], r["receivable_minor"], "institution_receivable_collection", r
            )
            r["collected"] = True
        elif method == "nonrecourse_assignment":
            ctx.require(
                not r.get("assigned") and (not r.get("collected")) and (not r.get("loan_minor")),
                "RECEIVABLE_NOT_ASSIGNABLE",
            )
            cash(
                ctx, r["account_id"], r["assignment_advance_minor"], "institution_factor_advance", r
            )
            r["assigned"] = True
            r["finance_fee_minor"] = r["receivable_minor"] - r["assignment_advance_minor"]
        else:
            ctx.require(
                not r.get("loan_minor") and (not r.get("assigned")), "FINANCE_ALREADY_DRAWN"
            )
            cash(ctx, r["account_id"], r["credit_line_minor"], "institution_credit_drawing", r)
            cash(ctx, r["account_id"], -r["credit_fee_minor"], "institution_credit_fee", r)
            r["loan_minor"] = r["credit_line_minor"]
            r["finance_fee_minor"] = r["credit_fee_minor"]
        return r

    @op(
        "pay_institution_payroll",
        "Pay institution employees from institution-owned operating cash. The actual payment minute remains visible even when submitted after the contractual due time.",
        {"finance_id": "string"},
    )
    def payroll(ctx, finance_id):
        r = ctx.record("xp_c_institution_finance", finance_id)
        ctx.require(not r.get("payroll_paid"), "PAYROLL_ALREADY_PAID")
        cash(ctx, r["account_id"], -r["payroll_minor"], "institution_payroll", r)
        r.update(payroll_paid=True, payroll_minute=ctx.minute)
        return r

    @op(
        "repay_institution_credit",
        "Repay drawn institutional credit principal from real operating cash. This does not forgive a remaining balance.",
        {"finance_id": "string", "amount_minor": "integer"},
    )
    def repay(ctx, finance_id, amount_minor):
        r = ctx.record("xp_c_institution_finance", finance_id)
        ctx.require(0 < amount_minor <= r.get("loan_minor", 0), "NO_MATCHING_LOAN")
        cash(ctx, r["account_id"], -amount_minor, "institution_credit_repayment", r)
        r["loan_minor"] -= amount_minor
        return r

    def collect(ctx, old, k):
        r = scoped(ctx, "xp_c_debtor_tranches", k["negotiation_id"])
        if r is None:
            return old(ctx, **k)
        b = ctx.record("xp_c_negotiations", k["negotiation_id"])
        original = b["receivable_minor"]
        b["receivable_minor"] = r["_first_minor"]
        result = old(ctx, **k)
        b["receivable_minor"] = original
        r.update(
            first_received_minor=r["_first_minor"],
            remaining_minor=original - r["_first_minor"],
            notice_received=True,
        )
        return result

    wrap("collect_committed_receivable", collect)

    @op(
        "assign_residual_receivable",
        "Assign only the actual remaining undisputed debtor tranche to the selected nonrecourse factor. The factor fee is deducted from its cash advance and the transferred claim can no longer be collected.",
        {"tranche_id": "string", "factor_id": "string"},
    )
    def factor(ctx, tranche_id, factor_id):
        r = ctx.record("xp_c_debtor_tranches", tranche_id)
        f = ctx.record("xp_c_factor_offers", factor_id)
        ctx.require(
            r.get("notice_received")
            and r.get("remaining_minor", 0) > f["fee_minor"]
            and (not r.get("assigned")),
            "NO_ASSIGNABLE_REMAINDER",
        )
        ctx.require(f["tranche_id"] == r["id"], "FACTOR_WRONG_DEBT")
        b = ctx.record("xp_c_negotiations", r["source_id"])
        plan = ctx.record("xp_c_negotiation_plans", b["adopted_plan_id"])
        ctx.require(
            plan["revised_due_minute"] <= f["latest_invoice_due_minute"],
            "FACTOR_INVOICE_DUE_OUTSIDE_SCOPE",
        )
        face = r["remaining_minor"]
        cash(ctx, r["account_id"], face - f["fee_minor"], "residual_factor_advance", r)
        r.update(
            assigned=True,
            assigned_face_minor=face,
            factor_fee_minor=f["fee_minor"],
            factor_id=factor_id,
            remaining_minor=0,
        )
        return r

    @op(
        "collect_later_debtor_tranche",
        "Collect the still-owned remainder at its contractual later date. An assigned tranche cannot also be collected.",
        {"tranche_id": "string"},
    )
    def later(ctx, tranche_id):
        r = ctx.record("xp_c_debtor_tranches", tranche_id)
        ctx.require(
            ctx.minute >= r["later_due_minute"]
            and r.get("remaining_minor", 0) > 0
            and (not r.get("assigned")),
            "LATER_TRANCHE_UNAVAILABLE",
        )
        amount = r["remaining_minor"]
        cash(ctx, r["account_id"], amount, "later_debtor_collection", r)
        r.update(later_collected_minor=amount, remaining_minor=0)
        return r

    def wallet_run(ctx, old, k):
        result = old(ctx, **k)
        r = scoped(ctx, "xp_c_wallet_payrolls", k["controller_id"])
        if r and (not r.get("notice_received")) and result["controller"]["purchase_paid"]:
            r.update(
                notice_received=True,
                payroll_minor=r["_payroll_minor"],
                notice_cycle=result["controller"]["cycles_run"],
            )
        return result

    wrap("run_wallet_observation", wallet_run)

    @op(
        "fund_wallet_payroll",
        "Transfer explicit operating cash into the payroll wallet, charging the disclosed manual funding fee. This is separate from configured automatic topups.",
        {"payroll_id": "string", "amount_minor": "integer"},
    )
    def fund(ctx, payroll_id, amount_minor):
        r = ctx.record("xp_c_wallet_payrolls", payroll_id)
        ctx.require(amount_minor > r["manual_fee_minor"], "FUNDING_TOO_SMALL")
        cash(ctx, r["account_id"], -amount_minor, "payroll_manual_funding", r)
        cash(
            ctx,
            r["wallet_account_id"],
            amount_minor - r["manual_fee_minor"],
            "payroll_manual_credit",
            r,
        )
        r["manual_fees_minor"] = r.get("manual_fees_minor", 0) + r["manual_fee_minor"]
        return r

    @op(
        "settle_wallet_payroll",
        "Pay the actual payroll notice from the wallet at its current controller cycle. A late bank-accepted payment retains its true execution cycle.",
        {"payroll_id": "string", "amount_minor": "integer"},
    )
    def wallet_payroll(ctx, payroll_id, amount_minor):
        r = ctx.record("xp_c_wallet_payrolls", payroll_id)
        ctx.require(r.get("notice_received") and (not r.get("paid")), "PAYROLL_NOTICE_REQUIRED")
        ctx.require(amount_minor == r["payroll_minor"], "PAYROLL_AMOUNT_MISMATCH")
        cash(ctx, r["wallet_account_id"], -amount_minor, "wallet_staff_payroll", r)
        ctrl = ctx.record("xp_c_controllers", r["source_id"])
        r.update(paid=True, paid_minor=amount_minor, paid_cycle=ctrl["cycles_run"])
        return r

    def event(ctx, e):
        if e["kind"] == "c_reasoning_exit_claim":
            return ctx.update(
                "xp_c_exit_merchant_claims",
                e["subject_id"],
                notice_received=True,
                actual_eur_minor=e["_payload"]["amount_minor"],
            )
        if e["kind"] in ("c_reasoning_institution_receivable", "c_reasoning_later_tranche"):
            return {"available": True, "subject_id": e["subject_id"]}
        return old_event(ctx, e)

    api.event_handler = event
