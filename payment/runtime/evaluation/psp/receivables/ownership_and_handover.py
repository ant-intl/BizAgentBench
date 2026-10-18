from copy import deepcopy

NUMBERS = {33, 31}
TABLE = {
    31: "quarantine_disputes",
    33: "supplier_payables",
}


def verify(world, contract, previous):
    n = contract["number"]
    if n not in NUMBERS:
        return previous(world, contract)
    original = contract["initial_world"]
    customer = original["_customer_id"]
    c = deepcopy(contract)

    def rr(w, t):
        return [r for r in w.get("xp_a_" + t, {}).values() if r["customer_id"] == customer]

    initial = rr(original, TABLE[n])
    actual = rr(world, TABLE[n])
    r = actual[0]
    src = initial[0]
    journals = [
        j
        for j in world.get("xp_journal", {}).values()
        if j.get("customer_id") == customer and j.get("record_type") == "cash_posting"
    ]

    def money(kind):
        return sum((j["amount_minor"] for j in journals if j["kind"] == kind))

    def evidence(target, purpose):
        originals = [
            f
            for f in original["_user_files"]
            if f["purpose"] == purpose and f["target_id"] == target
        ]
        return any(
            (
                s.get("customer_id") == customer
                and s["target_id"] == target
                and (s["purpose"] == purpose)
                and any(
                    (s["file_id"] == f["id"] and s["content"] == f["content"] for f in originals)
                )
                for s in world.get("xp_submissions", {}).values()
            )
        )

    result = previous(world, c)
    checks = result["checks"]
    if n == 31:
        checks[:] = [x for x in checks if x["name"] != "a.31.authenticated_disposal"]

    def add(name, ok, detail, why):
        checks.append(
            dict(
                name=f"a.{n}.diversity_{name}",
                passed=bool(ok),
                detail=detail,
                explanation=dict(check_zh=why, rule_documents=[f"PX-A-D{n}"]),
            )
        )

    if n == 31:
        bank = src["_bank"]
        inc = rr(world, "incoming")[0]
        principal = original["xp_a_incoming"][src["source_id"]]["amount_minor"]
        disputed = bank["disputed_minor"]
        add(
            "rights",
            r.get("disposed")
            and r.get("returned_minor") == principal - disputed
            and (r.get("escrowed_minor") == disputed)
            and (r.get("claimed_minor") == 0)
            and (not disputed or r.get("paid_trustee_id") == bank["authorized_trustee_id"]),
            r,
            "The disputed principal agreed by both parties remains in the named escrow account, The rest returned via original route, Do not divert to operating funds or waive collateral",
        )
        add(
            "ledger",
            world["xp_accounts"][src["quarantine_account_id"]]["balance_minor"] == 0
            and world["xp_accounts"][bank["authorized_trustee_id"]]["balance_minor"] == disputed
            and (money("QUARANTINE_CLAIM") == 0)
            and (money("DISPUTE_ESCROW_DEPOSIT") == disputed),
            r.get("escrowed_minor"),
            "Sub-accounting must occur in real independent cash accounts, Cannot merely change the disposition label",
        )
        add(
            "evidence",
            evidence(src["source_id"], "escrow_direction")
            and evidence(src["source_id"], "origin_proof")
            and r.get("review_complete")
            and inc.get("ownership_investigation"),
            r.get("review_complete"),
            "Original remittance source and escrow documents genuinely signed by both parties are each verified.",
        )
    elif n == 33:
        posts = rr(world, "payable_postings")
        receipts = rr(world, "bank_receipts")
        valid = True
        expected_lines = []
        for payable in actual:
            latest = max(
                (x for x in receipts if x["invoice_id"] == payable["source_id"]),
                key=lambda x: x["bank_sequence"],
            )
            total = sum((x["amount_minor"] for x in posts if x["payable_id"] == payable["id"]))
            valid &= (
                payable["open_minor"] == 0
                and total == 0
                and (payable.get("source_sequence") == latest["bank_sequence"])
                and (payable.get("source_receipt_id") == latest["id"])
            )
            expected_lines.append(
                dict(
                    payable_id=payable["id"],
                    invoice_id=payable["source_id"],
                    open_minor=0,
                    source_sequence=latest["bank_sequence"],
                )
            )
        add(
            "rights",
            valid,
            actual,
            "The highest causal bank receipt drives the actual payable reopening, Substitute payment offsets the same debt; old in-processing notifications cannot create liabilities",
        )
        statements = {s["id"]: s for s in rr(world, "supplier_statements")}
        requests = world.get("xp_requests", {})
        delivered = []
        for audit in sorted(world.get("audit", []), key=lambda x: x.get("seq", 0)):
            if audit.get("tool") != "execute_request" or not audit.get("result", {}).get("ok"):
                continue
            request = requests.get(audit.get("arguments", {}).get("request_id"), {})
            statement_id = audit.get("result", {}).get("data", {}).get("id")
            if (
                request.get("operation") == "a_deliver_supplier_payable_statement"
                and request.get("executed_seq") == audit.get("seq")
                and (statement_id in statements)
            ):
                delivered.append((audit["seq"], statement_id))
        latest = statements[delivered[-1][1]] if delivered else None
        correct = bool(
            latest
            and latest.get("delivered")
            and (
                sorted(latest["lines"], key=lambda l: l["payable_id"])
                == sorted(expected_lines, key=lambda l: l["payable_id"])
            )
        )
        add(
            "statement",
            correct,
            dict(latest=latest, expected=expected_lines),
            "The final valid delivered supplier statement must cover both obligations and accurately reflect the settled balance and causal source; An earlier correct report cannot make a later erroneous delivery pass",
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda world, contract: verify(world, contract, previous)
