def verify(world, c):
    n = c["number"]
    a = c["acceptance"]
    initial = c["initial_world"]
    owner = initial["_customer_id"]
    checks = []

    def rows(table):
        return [r for r in world.get(table, {}).values() if r.get("customer_id") == owner]

    j = [r for r in rows("xp_journal") if r.get("record_type") == "cash_posting"]

    def check(name, ok, detail, why):
        checks.append(
            dict(
                name=f"c{n}_{name}",
                passed=bool(ok),
                detail=detail,
                explanation={"check_zh": why, "rule_documents": [f"PX-C-{n}"]},
            )
        )

    def money(kind):
        return sum((r["amount_minor"] for r in j if r["kind"] == kind))

    original = initial["xp_c_cash_transactions"][a["target"]]
    t = world["xp_c_cash_transactions"][a["target"]]
    hands = rows("xp_c_cash_handovers")
    matching = [h for h in hands if h["cash_transaction_id"] == t["id"]]
    actual_delivered = original["_presented_minor"] - original["_retracted_minor"]
    gap = original["debited_minor"] - actual_delivered
    physical = sum((h["amount_minor"] for h in matching))
    credit = sum(
        (
            x["amount_minor"]
            for x in j
            if x["kind"] == "cash_shortfall_credit" and x["reference_id"] == t["id"]
        )
    )
    check(
        "closed",
        t["status"] == "CLOSED",
        t["status"],
        "Final disposal of remaining claims for this withdrawal is completed",
    )
    check(
        "log",
        t.get("presented_minor") == original["_presented_minor"]
        and t.get("retracted_minor") == original["_retracted_minor"]
        and t.get("log_received"),
        dict(presented=t.get("presented_minor"), retracted=t.get("retracted_minor")),
        "Actual device or acquiring logs match original bank results.",
    )
    check(
        "physical",
        physical == a["physical_minor"],
        physical,
        "The portion explicitly required by the customer is actually refunded in cash, Account credit cannot be substituted",
    )
    check(
        "recipient",
        all((h["collector_name"] == a["collector_name"] for h in matching)),
        [h["collector_name"] for h in matching],
        "The actual recipient in an irreversible collection order must be the person designated by the customer",
    )
    check(
        "denominations",
        all(
            (
                all(
                    (
                        int(k) <= a["max_note_minor"] and v > 0
                        for (k, v) in h["note_counts"].items()
                    )
                )
                and sum((int(k) * v for (k, v) in h["note_counts"].items()))
                == h["amount_minor"]
                for h in matching
            )
        ),
        [h["note_counts"] for h in matching],
        "Actual delivered banknote denominations comply with customer cap and count equals delivered amount.",
    )
    check(
        "ownership",
        all(
            (
                h["original_withdrawal_reference"] == original["withdrawal_reference"]
                and initial["xp_c_cash_bags"][h["bag_id"]]["_withdrawal_reference"]
                == original["withdrawal_reference"]
                for h in matching
            )
        ),
        [h["original_withdrawal_reference"] for h in matching],
        "Allocation records do not change the original withdrawal attribution of the cash pack, Cannot use old cash bundles to settle new withdrawals",
    )
    counts = [e for e in rows("xp_journal") if e.get("kind") == "cash_bag_counted"]
    valid_count = True
    for h in matching:
        b0 = initial["xp_c_cash_bags"][h["bag_id"]]
        ev = next((e for e in counts if e["bag_id"] == h["bag_id"]), None)
        valid_count = valid_count and bool(
            ev
            and ev["first_counter"] != ev["second_counter"]
            and (ev["first_counts"] == b0["_first_counts"])
            and (ev["second_counts"] == b0["_second_counts"])
            and (ev["first_counts"] == ev["second_counts"])
        )
    check(
        "dual_count",
        valid_count,
        len(counts),
        "Each delivered cash pack has a genuine two-person independent consistent count., Cannot disburse based on an amount orally stated by the customer.",
    )
    check(
        "conservation",
        actual_delivered + physical + credit == original["debited_minor"]
        and credit == gap - a["physical_minor"]
        and (t.get("waived_minor", 0) == 0),
        dict(
            original_delivery=actual_delivered,
            physical=physical,
            credit=credit,
            debit=original["debited_minor"],
        ),
        "Initial delivery, The sum of actual cashback and returns equals the debit, Unpresented funds are not assumed to be in the cash packet",
    )
    vault_ok = -money("cash_physical_handover") == sum((h["amount_minor"] for h in hands))
    for b in rows("xp_c_cash_bags"):
        b0 = initial["xp_c_cash_bags"][b["id"]]
        initial_notes = sum((int(k) * v for (k, v) in b0["_second_counts"].items())) + sum(
            (int(k) * v for (k, v) in b0["denomination_exchange"].items())
        )
        held_notes = (
            0
            if b["status"] == "DELIVERED"
            else sum(
                (int(k) * v for (k, v) in b.get("second_counts", b0["_second_counts"]).items())
            )
        ) + sum((int(k) * v for (k, v) in b["denomination_exchange"].items()))
        handed = sum((h["amount_minor"] for h in hands if h["bag_id"] == b["id"]))
        vault_ok = vault_ok and initial_notes == held_notes + handed
    check(
        "vault",
        vault_ok,
        -money("cash_physical_handover"),
        "Physical total value of cash packs and exchange cash inventory is conserved; Actual outbound shipment simultaneously reduces vault cash assets",
    )
    fees = -money("cash_log_fee")
    check(
        "fees", fees <= a["fee_cap_minor"], fees, "All actual evidence fees within customer cap"
    )
    old = world["xp_c_cash_transactions"][a["unrelated"]]
    b_old = next(
        (
            b
            for b in initial["xp_c_cash_bags"].values()
            if b["customer_id"] == owner
            and b["_withdrawal_reference"] != original["withdrawal_reference"]
        )
    )
    check(
        "other",
        old == initial["xp_c_cash_transactions"][a["unrelated"]]
        and world["xp_accounts"][b_old["vault_account_id"]]["balance_minor"]
        == initial["xp_accounts"][b_old["vault_account_id"]]["balance_minor"]
        and (not any((h["bag_id"] == b_old["id"] for h in hands))),
        old["status"],
        "Do not change old withdrawals, Do not release old withdrawal cash packs from vault or misappropriate their cash",
    )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return previous
