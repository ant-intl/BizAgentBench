from payment.runtime.evaluation.psp.accounts.observations import initial_record

TABLE = "xp_b_reasoning_supplier_schedule"
PAY = "xp_b_reasoning_fx_installments"
DOCS = ["PX-BR-FX-INSTALLMENTS", "PX-BR-FX-CREDIT-PORTIONS", "PX-BR-FX-EXPOSURE"]


def verify(w, c, base):
    checks = [
        x
        for x in base["checks"]
        if x["name"]
        not in ("B-36-goal", "B-36-integrity", "B-36-coupling", "B-36-supplier_deadline")
    ]
    initial = initial_record(c, TABLE)
    cid = initial["customer_id"]
    rows = lambda t: [x for x in w.get(t, {}).values() if x.get("customer_id") == cid]
    q = w["xp_b_fx_requirements"][initial["requirement_id"]]
    credit = w["xp_b_supplier_fx_credits"][initial["credit_record_id"]]
    oldcredit = c["initial_world"]["xp_b_supplier_fx_credits"][credit["id"]]
    order = w["xp_b_fx_orders"][initial["order_id"]]
    installments = [x for x in rows(PAY) if x["requirement_id"] == q["id"]]

    def add(n, ok, d, t):
        checks.append(
            dict(
                name="BR-36-" + n,
                passed=bool(ok),
                detail=d,
                explanation=dict(check_zh=t, rule_documents=DOCS),
            )
        )

    advance = sum((x["amount_minor"] for x in installments))
    early = sum(
        (
            x["amount_minor"]
            for x in installments
            if x["received_minute"] <= initial["advance_deadline"]
        )
    )
    offset = credit.get("applied_credit_minor", 0)
    refund = credit.get("cash_elected_minor", 0)
    cash = credit.get("refund_received_minor", 0)
    fee = oldcredit["refund_fee_minor"] if refund else 0
    final = q["need_minor"] if q.get("paid") else 0
    add(
        "invoice",
        early >= initial["advance_minimum_minor"]
        and advance + final + offset == initial["original_face_minor"]
        and q.get("paid")
        and (q.get("paid_minute", 999) <= initial["final_deadline"]),
        dict(early=early, installments=advance, final=final, offset=offset),
        "By actual early installments, Final payment and non-duplicate deduction recalculate the same original invoice, Meet the two deadlines separately",
    )
    posted = sum(
        (
            j["amount_minor"]
            for j in rows("xp_journal")
            if j.get("record_type") == "cash_posting"
            and j.get("kind") == "supplier_credit_cash_refund"
        )
    )
    expected_cash = max(0, refund * q["rate_num"] // q["rate_den"] - fee)
    refund_journal = [
        j
        for j in rows("xp_journal")
        if j.get("record_type") == "cash_posting" and j.get("kind") == "supplier_credit_cash_refund"
    ]
    add(
        "credit",
        offset + refund == oldcredit["_credit_minor"]
        and offset <= initial["original_face_minor"] - advance
        and (cash == expected_cash == posted)
        and all((j["currency"] == q.get("source_currency", "USD") for j in refund_journal)),
        dict(
            actual_credit=oldcredit["_credit_minor"],
            offset_eur_minor=offset,
            refund_face_eur_minor=refund,
            refund_cash_usd_minor=cash,
            fee_usd_minor=fee,
        ),
        "The EUR credit is exactly split into an offset against the original unpaid amount and a USD cash refund converted at the public quote, Do not encroach on paid installments or reuse the same credit item",
    )
    add(
        "currency",
        q.get("currency") == "EUR"
        and q.get("source_currency") == "USD"
        and (w["xp_accounts"][q["account_id"]]["currency"] == "USD"),
        dict(obligation_currency=q.get("currency"), source_currency=q.get("source_currency")),
        "Original invoice and payment receipt based on EUR Calculate, Purchase, Refund, Fees and source cash are based on USD Calculate. ",
    )
    buys = sum(
        (
            x["amount_minor"]
            for x in rows("xp_b_history")
            if x.get("kind") == "fx_bought" and x.get("subject_id") == q["id"]
        )
    )
    stock = order["filled_minor"] + buys - credit.get("sold_minor", 0) - advance
    unrelated = c["acceptance"]["untouched"]
    add(
        "stock",
        order["status"] == "cancelled"
        and q["acquired_minor"] == stock == q["need_minor"]
        and (q["remaining_minor"] == 0)
        and (w["xp_b_fx_orders"][unrelated] == c["initial_world"]["xp_b_fx_orders"][unrelated])
        and credit.get("confirmed"),
        dict(
            fills=order["filled_minor"],
            buys=buys,
            sold=credit.get("sold_minor", 0),
            early_spent=advance,
            final_stock=stock,
        ),
        "Per-transaction foreign currency source minus actual advance payments and FX sales, Confirm the remaining amount exactly supports the final payment and no unrelated order changes",
    )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
