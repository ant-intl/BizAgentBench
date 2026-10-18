TABLES = {
    34: "xp_b_order_fulfillments",
    35: "xp_b_platform_reconciliations",
    36: "xp_b_supplier_fx_credits",
    37: "xp_b_royalty_tax",
    38: "xp_b_jurisdiction_credits",
    39: "xp_b_customs_liens",
    40: "xp_b_proof_access",
    41: "xp_b_supplier_beneficiaries",
    42: "xp_b_credential_continuity",
}


def verify(world, contract, result):
    n = contract["number"]
    table = TABLES[n]
    rid = contract["acceptance"]["enhancement_record_id"]
    r = world.get(table, {}).get(rid, {})
    initial = contract["initial_world"][table][rid]
    checks = list(result["checks"])

    def add(name, ok, detail, reason):
        checks.append(
            {
                "name": f"B-{n}-{name}",
                "passed": bool(ok),
                "detail": detail,
                "explanation": {"check_zh": reason, "rule_documents": [f"PX-B-{n}"]},
            }
        )

    if n == 36:
        q = world["xp_b_fx_requirements"][r["requirement_id"]]
        credit = initial["_credit_minor"]
        netted = r.get("applied_credit_minor", 0)
        refund = r.get("refund_received_minor", 0)
        choice = r.get("credit_choice")
        valid_credit = (
            choice == "invoice_netting"
            and netted == credit
            and (refund == 0)
            or (
                choice == "cash_refund"
                and netted == 0
                and (refund == max(0, credit - initial["refund_fee_minor"]))
            )
        )
        events = [
            e
            for e in world.get("xp_events", {}).values()
            if e.get("subject_id") == rid
            and e.get("kind") == "b_enhance_supplier_credit"
            and (e.get("status") == "COMPLETED")
        ]
        cash = [
            j
            for j in world.get("xp_journal", {}).values()
            if j.get("customer_id") == r.get("customer_id")
            and j.get("record_type") == "cash_posting"
        ]
        sold = [
            j
            for j in world.get("xp_journal", {}).values()
            if j.get("subject_id") == rid and j.get("kind") == "supplier_currency_sold"
        ]
        expected_sale = sum((x.get("sale_proceeds_minor", 0) for x in sold))
        actual_sale = sum(
            (j["amount_minor"] for j in cash if j["kind"] == "supplier_fx_liquidation")
        )
        add(
            "coupling",
            bool(events)
            and r.get("credit_applied")
            and valid_credit
            and (q["need_minor"] == initial["original_need_minor"] - netted)
            and r.get("confirmed")
            and q.get("paid")
            and (r.get("confirmed_payable_minor") == q["need_minor"])
            and (q["remaining_minor"] == 0),
            {
                "credit": credit,
                "disposition": choice,
                "netted": netted,
                "refund": refund,
                "payable": q["need_minor"],
            },
            "The same supplier credit must actually offset current payables or be returned after deduction., Original payment and credit must not be completed independently",
        )
        add(
            "liquidation",
            actual_sale == expected_sale
            and sum((j["amount_minor"] for j in cash if j["kind"] == "supplier_credit_cash_refund"))
            == refund,
            {"sale_receipts": actual_sale, "sale_business_events": expected_sale},
            "Target currency sale has actual cash receipt; Refund cannot be reused with current-period offset",
        )
        payment_requests = [
            x
            for x in world.get("xp_requests", {}).values()
            if x.get("customer_id") == r["customer_id"]
            and x.get("operation") == "pay_foreign_obligation"
            and (x.get("status") == "EXECUTED")
        ]
        add(
            "supplier_deadline",
            len(payment_requests) == 1
            and payment_requests[0]["executed_minute"] <= initial["supplier_deadline"],
            [x.get("executed_minute") for x in payment_requests],
            "Credit pending and cancellation do not exempt the supplier’s original payment deadline",
        )
    elif n == 38:
        credit = initial["_credit_minor"]
        allocated = r.get("allocated_minor", 0)
        target = world.get("xp_b_tax_obligations", {}).get(r.get("allocated_obligation_id"), {})
        valid_allocation = (
            credit == 0
            and allocated == 0
            or (
                allocated == credit and target.get("jurisdiction") == initial["credit_jurisdiction"]
            )
        )
        receipts = r.get("receipts", {})
        valid_receipts = True
        for oid in initial["obligation_ids"]:
            original = contract["initial_world"]["xp_b_tax_obligations"][oid]
            actual = world["xp_b_tax_obligations"][oid]
            receipt = receipts.get(oid, {})
            eligible_credit = (
                credit if original["jurisdiction"] == initial["credit_jurisdiction"] else 0
            )
            valid_receipts &= (
                actual.get("amount_minor") == original["amount_minor"] - eligible_credit
                and receipt.get("cash_minor") == actual["amount_minor"]
                and (receipt.get("credit_minor") == eligible_credit)
                and (receipt.get("jurisdiction") == original["jurisdiction"])
                and (receipt.get("recipient_id") == actual.get("recipient_id"))
            )
        add(
            "coupling",
            r.get("credit_confirmed")
            and valid_allocation
            and valid_receipts
            and (set(receipts) == set(initial["obligation_ids"])),
            {
                "credit": credit,
                "allocated": allocated,
                "target_jurisdiction": target.get("jurisdiction"),
                "receipts": receipts,
            },
            "Independently recalculate cash and per original jurisdiction’s tax liability credit, A correct two-tax total cannot mask cross-jurisdiction mismatches or omitted receipts",
        )
    elif n == 41:
        expected = (
            initial["_confirmed_beneficiary_id"]
            if initial["amendment_pending"]
            else initial["original_beneficiary_id"]
        )
        payment = world["xp_b_secure_obligations"][initial["obligation_id"]]
        challenge = world.get("xp_b_payment_challenges", {}).get(payment.get("challenge_id"), {})
        verified = not initial["amendment_pending"] or r.get("verification_complete")
        add(
            "coupling",
            verified
            and r.get("authorized_beneficiary_id") == expected
            and (payment.get("beneficiary_id") == expected)
            and (challenge.get("beneficiary_id") == expected)
            and r.get("supplier_receipt_received")
            and (r.get("credited_beneficiary_id") == expected)
            and (r.get("credited_minor") == payment["amount_minor"]),
            {
                "confirmed_supplier": expected,
                "authorized": r.get("authorized_beneficiary_id"),
                "paid_to": payment.get("beneficiary_id"),
                "receipt_to": r.get("credited_beneficiary_id"),
            },
            "The payee entity is verified through registered channels., Original challenge., Original payment and actual receipt must both be bound to the same real supplier, Post-event authorization changes cannot fix mistaken payments",
        )
    else:
        from payment.runtime.evaluation.psp.accounts.evidence_checks import verify_extra

        verify_extra(world, contract, add)
    return {"success": all((x["passed"] for x in checks)), "checks": checks}
