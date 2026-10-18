from payment.runtime.evaluation.psp.accounts.observations import initial_record

TABLE = "xp_b_reasoning_royalty_delivery"
CHANNELS = "xp_b_reasoning_royalty_channels"
BOOKINGS = "xp_b_reasoning_royalty_bookings"
DOCS = ["PX-BR-ROYALTY-CAPACITY", "PX-BR-ROYALTY-RECEIPTS", "PX-BR-ROYALTY-REVIEW"]


def verify(w, c, base):
    checks = list(base["checks"])
    cid = c["initial_world"].get(
        "_customer_id", c["initial_world"]["xp_cases"][c["id"]]["customer_id"]
    )
    original = initial_record(c, TABLE)
    rows = lambda t: [x for x in w.get(t, {}).values() if x.get("customer_id") == cid]
    r = next(iter((w[TABLE][x["id"]] for x in rows(TABLE))))
    allocs = rows("xp_b_allocations")
    tax = rows("xp_b_royalty_tax")[0]
    bookings = rows(BOOKINGS)
    chs = c["initial_world"][CHANNELS]

    def add(name, ok, detail, reason):
        checks.append(
            dict(
                name="BR-37-" + name,
                passed=bool(ok),
                detail=detail,
                explanation=dict(check_zh=reason, rule_documents=DOCS),
            )
        )

    pool = c["initial_world"]["xp_b_pools"][original["pool_id"]]
    total = pool["source_minor"] * pool["rate_num"] // pool["rate_den"]
    den = sum((a["weight"] for a in allocs))
    amounts = {a["id"]: total * a["weight"] // den for a in allocs}
    rank = sorted(allocs, key=lambda a: (-(total * a["weight"] % den), a["residual_rank"]))
    for a in rank[: total - sum(amounts.values())]:
        amounts[a["id"]] += 1
    received = {
        a["id"]: sum(
            (b.get("received_minor", 0) for b in bookings if b["allocation_id"] == a["id"])
        )
        for a in allocs
    }
    timely = all(
        (
            b.get("receipt_minute", 999)
            <= original["beneficiary_conditions"][b["allocation_id"]]["deadline"]
            for b in bookings
            if b.get("amount_minor")
        )
    )
    balanced = all(
        (
            received[a["id"]]
            + (tax.get("withheld_minor", 0) if tax.get("withheld_beneficiary_id") == a["id"] else 0)
            == amounts[a["id"]]
            for a in allocs
        )
    )
    add(
        "receipts",
        balanced and timely and all((b["status"] in ("cancelled", "delivered") for b in bookings)),
        {"received": received, "gross_rights": amounts, "timely": timely},
        "Recalculate original entitlements based on actual receipts per person and actual personal withholding., Simultaneously verify respective arrival deadlines; Submitted amount is not counted as received amount.",
    )
    capacities = all(
        (
            sum((b["capacity_group"] == group and b["status"] != "cancelled" for b in bookings))
            <= limit
            for (group, limit) in original["capacity_limits"].items()
        )
    )
    fees = sum((chs[b["channel_id"]]["fee_minor"] for b in bookings))
    posted = -sum(
        (
            j["amount_minor"]
            for j in rows("xp_journal")
            if j.get("record_type") == "cash_posting"
            and j.get("kind") == "royalty_delivery_booking_fee"
        )
    )
    add(
        "resources",
        capacities and fees == posted and (fees <= original["fee_budget_minor"]),
        {"capacity_limits": original["capacity_limits"], "fees": fees, "actual_fee_cash": posted},
        "instant service shared instruction count, All reservations, including cancelled service fees, occupy the original cash budget",
    )
    eligibility = True
    for b in bookings:
        if b.get("received_minor", 0):
            cond = (
                original["_final_beneficiary_conditions"]
                if b["submitted_minute"] >= r.get("conditions_minute", 999)
                else original["beneficiary_conditions"]
            )[b["allocation_id"]]
            ch = chs[b["channel_id"]]
            eligibility &= b["channel_id"] in cond["allowed_channels"] and (
                not ch["requires_final_conditions"]
                or b["submitted_minute"] >= r.get("conditions_minute", 999)
            )
    add(
        "effective_route",
        eligibility,
        {},
        "Each payment is verified against the receiving conditions in effect at the time of actual submission., Later qualified results do not retroactively certify earlier unqualified instructions",
    )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
