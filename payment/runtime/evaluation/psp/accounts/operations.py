from payment.runtime.evaluation.psp.accounts import obligations as _enhancements
from payment.runtime.evaluation.psp.accounts import tax_and_authority as _diversity_revision
from payment.runtime.evaluation.psp.accounts import dependencies as _reasoning_revision


def verify(world, contract):
    """Independent equations and invariants, without calling runtime transitions."""
    n = contract["number"]
    a = contract["acceptance"]
    initial = contract["initial_world"]
    checks = []

    def rows(t):
        return [
            r
            for r in world.get(t, {}).values()
            if r.get("customer_id") in (world.get("_customer_id"), "SHARED")
        ]

    def rec(t, i):
        return world.get(t, {}).get(i, {})

    def old(t, i):
        return initial.get(t, {}).get(i, {})

    def add(s, ok, detail, zh):
        checks.append(
            {
                "name": f"B-{n}-{s}",
                "passed": bool(ok),
                "detail": detail,
                "explanation": {"check_zh": zh, "rule_documents": [f"PX-B-{n}"]},
            }
        )

    cash = [j for j in rows("xp_journal") if j.get("record_type") == "cash_posting"]
    spending = -sum((j["amount_minor"] for j in cash if j["amount_minor"] < 0))
    add(
        "budget",
        spending <= a["budget_minor"],
        {"spent_minor": spending, "budget_minor": a["budget_minor"]},
        "Executed cash expenditures do not exceed the budget",
    )
    if n == 34:
        r = rec("xp_b_store_orders", a["target"])
        p = rec("xp_b_gateway_payments", a["payment_id"])
        route = rec("xp_b_gateway_routes", p.get("route_id"))
        h = rows("xp_b_history")
        add(
            "goal",
            r.get("status") == "paid"
            and set(r.get("receipt_payment_ids", [])) == set(initial["xp_b_gateway_payments"])
            and (r.get("net_received_minor") == r.get("amount_minor"))
            and (route.get("merchant_account") == p.get("merchant_account")),
            r,
            "Actual clearing receipt supports the correct merchant order",
        )
        add(
            "integrity",
            rec("xp_b_store_orders", a["untouched"]) == old("xp_b_store_orders", a["untouched"])
            and (not any((x["kind"] == "store_recharged" for x in h)))
            and (not any((j["kind"] == "new_store_charge" for j in cash))),
            h,
            "No duplicate deductions, No disclosure of receipts to historical merchants",
        )
    elif n == 35:
        rule = rec("xp_b_payout_rules", a["rule"])
        pay = rec("xp_b_platform_payouts", a["payout"])
        account = rec("xp_b_receiving_accounts", a["receiving"])
        outstanding = [
            e
            for e in rows("xp_events")
            if e["kind"] == "b_payout_rule"
            and e["subject_id"] == a["rule"]
            and (e["status"] == "SCHEDULED")
        ]
        add(
            "goal",
            rule.get("enabled") is False
            and rule.get("pending_enabled") is None
            and (not outstanding)
            and (pay.get("status") == "settled")
            and all((x["status"] == "settled" for x in rows("xp_b_platform_payouts"))),
            {"rule": rule, "payout": pay, "outstanding_changes": outstanding},
            "Platform deactivation confirmed, No changes pending to take effect and accepted principal has completed posting; No additional fixed observation time required",
        )
        credits = sum((j["amount_minor"] for j in cash if j["kind"] == "accepted_payout"))
        expected = sum(
            (
                x["amount_minor"]
                for x in initial["xp_b_platform_payouts"].values()
                if x["status"] == "accepted"
            )
        ) + sum(
            (
                e["_payload"]["amount_minor"]
                for e in rows("xp_events")
                if e["kind"] == "b_platform_acceptance"
                and e["status"] == "COMPLETED"
                and (e.get("disposition") == "accepted")
            )
        )
        add(
            "integrity",
            account.get("status") == "active"
            and rec("xp_b_payout_rules", a["preserve_rule"])
            == old("xp_b_payout_rules", a["preserve_rule"])
            and (credits == expected),
            {"credit_minor": credits, "accepted_principal_minor": expected},
            "Retain manual collection and other rules.; Actual receipts equal original in-transit principal plus actual newly accepted principal, deactivation time not bound to a reference policy",
        )
    elif n == 36:
        r = rec("xp_b_fx_requirements", a["target"])
        o = rec("xp_b_fx_orders", a["order"])
        h = [x for x in rows("xp_b_history") if x["kind"] == "fx_bought"]
        sold = sum(
            (
                x.get("sold_minor", 0)
                for x in rows("xp_b_supplier_fx_credits")
                if x.get("requirement_id") == a["target"]
            )
        )
        total = o.get("filled_minor", 0) + sum((x["amount_minor"] for x in h)) - sold
        add(
            "goal",
            o.get("status") == "cancelled"
            and r.get("paid")
            and (total == r.get("need_minor"))
            and (r.get("remaining_minor") == 0),
            {"total_bought": total, "required": r.get("need_minor")},
            "Confirm that after order cancellation total executed amount exactly covers actual payment, No over-exchange exposure",
        )
        add(
            "integrity",
            r.get("acquired_minor") == total
            and rec("xp_b_fx_orders", a["untouched"]) == old("xp_b_fx_orders", a["untouched"]),
            {},
            "Transaction and spot exchange purchase are conserved, Unrelated irrevocable products remain unchanged",
        )
    elif n == 37:
        p = rec("xp_b_pools", a["pool"])
        o = old("xp_b_pools", a["pool"])
        aa = rows("xp_b_allocations")
        total = o["source_minor"] * o["rate_num"] // o["rate_den"]
        den = sum((x["weight"] for x in aa))
        rights = {x["id"]: total * x["weight"] // den for x in aa}
        rank = sorted(aa, key=lambda x: (-(total * x["weight"] % den), x["residual_rank"]))
        for x in rank[: total - sum(rights.values())]:
            rights[x["id"]] += 1
        add(
            "goal",
            all(
                (
                    x["paid_minor"]
                    + sum(
                        (
                            t.get("withheld_minor", 0)
                            for t in rows("xp_b_royalty_tax")
                            if t.get("withheld_beneficiary_id") == x["id"]
                        )
                    )
                    == rights[x["id"]]
                    for x in aa
                )
            )
            and p.get("remaining_minor") == 0,
            {"rights": rights, "paid": {x["id"]: x["paid_minor"] for x in aa}},
            "Allocate each person’s discrete entitlement by exact largest remainder method",
        )
        add(
            "integrity",
            sum((x["paid_minor"] for x in aa))
            + sum((t.get("withheld_minor", 0) for t in rows("xp_b_royalty_tax")))
            + p.get("remaining_minor", 0)
            == total
            and p.get("target_total_minor") == total,
            {},
            "Conversion total, Per-person payment and remaining balance conservation",
        )
    elif n == 38:
        obs = [rec("xp_b_tax_obligations", i) for i in a["obligations"]]
        valid = True
        for o in obs:
            p = rec("xp_b_tax_agents", o.get("recipient_id"))
            valid &= bool(
                o.get("paid")
                and o.get("discharged")
                and (o.get("paid_minute", 999) <= o["deadline"])
                and (o["jurisdiction"] in p.get("jurisdictions", []))
                and (p.get("kind") == "authority" or p.get("configured"))
            )
        add("goal", valid, obs, "Each tax is paid timely to the valid recipient for that tax")
        add(
            "integrity",
            rec("xp_b_tax_agents", "AGENT-OLD") == old("xp_b_tax_agents", "AGENT-OLD"),
            {},
            "Historical tax agent identity and permissions are not overwritten",
        )
    elif n == 39:
        r = rec("xp_b_importers", a["importer"])
        d = rec("xp_b_destinations", a["destination"])
        eligible = sum(
            (x["duty_minor"] for x in initial["xp_b_customs_entries"].values() if x["_eligible"])
        )
        add(
            "goal",
            r.get("award_status") == "received"
            and r.get("award_minor") == eligible
            and (
                sum(
                    (
                        rec("xp_b_destinations", i).get("received_minor", 0)
                        for i in a["allowed_destinations"]
                    )
                )
                == eligible
            ),
            {"eligible_minor": eligible, "award": r.get("award_minor")},
            "Refunds for all eligible import records actually enter the enterprise. ACH Account",
        )
        add(
            "integrity",
            r.get("destination_id") in a["allowed_destinations"]
            and rec("xp_b_destinations", r.get("destination_id")).get("owner")
            == r.get("legal_name")
            and all(
                (
                    x.get("received_minor", 0) == 0
                    for x in rows("xp_b_destinations")
                    if x["id"] not in a["allowed_destinations"]
                )
            ),
            {},
            "Claimant, Record that importer and payee are the same and the agent has no funds",
        )
    elif n == 40:
        r = rec("xp_b_evidence_requests", a["request"])
        ds = [x for x in rows("xp_b_bank_evidence") if x["delivered"]]
        accepted = any(
            (
                x["kind"] == r["required_kind"]
                and x["sealed"]
                and (set(r["required_fields"]) <= set(x["field_names"]))
                and (r["transaction_id"] in x["transaction_ids"])
                for x in ds
            )
        )
        add(
            "goal",
            accepted,
            ds,
            "Proof of delivery retains payment facts and signature/seal required for acceptance",
        )
        add(
            "integrity",
            all(
                (
                    set(x["field_names"]) <= set(a["allowed_fields"])
                    and set(x["transaction_ids"]) <= set(a["allowed_transactions"])
                    for x in ds
                )
            )
            and world.get("xp_b_original_transactions")
            == initial.get("xp_b_original_transactions"),
            {},
            "Every delivery is within field and transaction authorization scope, Original records unchanged.",
        )
    elif n == 41:
        r = rec("xp_b_secure_obligations", a["target"])
        p = rec("xp_b_access_profiles", a["profile"])
        h = [
            x
            for x in rows("xp_b_history")
            if x["kind"] == "authenticated_payment" and x["subject_id"] == r.get("id")
        ]
        baseline = old("xp_b_access_profiles", a["profile"])
        paid_minute = r.get("paid_minute", -1)
        prior_recoveries = [
            event
            for event in rows("xp_events")
            if event["kind"] == "b_access_recovery"
            and event["subject_id"] == a["profile"]
            and (event.get("status") == "COMPLETED")
            and (event.get("_payload", {}).get("_channel") == "payment")
            and (event.get("completed_minute", 999) <= paid_minute)
        ]
        execution_version = baseline["payment_version"] + len(prior_recoveries)
        active_at_execution = baseline["payment_status"] == "active" or bool(prior_recoveries)
        challenge = rec("xp_b_payment_challenges", r.get("challenge_id"))
        valid_execution = (
            len(h) == 1
            and h[0]["credential_version"] == execution_version
            and (h[0]["minute"] == paid_minute)
            and (r.get("credential_version") == execution_version)
            and (challenge.get("credential_version") == execution_version)
            and (challenge.get("status") == "consumed")
            and (challenge.get("used_minute") == paid_minute)
            and (challenge.get("expires_minute", -1) >= paid_minute)
        )
        add(
            "goal",
            r.get("paid")
            and paid_minute <= r["deadline"]
            and active_at_execution
            and valid_execution,
            {
                "payment": r,
                "execution_version": execution_version,
                "final_version": p.get("payment_version"),
            },
            "According to the actual recovery event at payment execution, Voucher version and binding challenge verification, timely payment; legal voucher restoration after payment does not retroactively void paid obligations",
        )
        add(
            "integrity",
            len(h) == 1
            and (
                h[0]["credential_version"]
                >= old("xp_b_access_profiles", a["profile"])["payment_version"]
                if a.get("already_recovered")
                else h[0]["credential_version"]
                > old("xp_b_access_profiles", a["profile"])["payment_version"]
            )
            and (
                rec("xp_b_secure_obligations", a["untouched"])
                == old("xp_b_secure_obligations", a["untouched"])
            ),
            h,
            "Payment has new identity execution evidence and old quote was not mistakenly paid",
        )
    elif n == 42:
        r = rec("xp_b_security_transfers", a["legitimate"])
        cs = rows("xp_b_credentials")
        bad = [rec("xp_b_security_transfers", i) for i in a["suspicious"]]
        add(
            "goal",
            r.get("status") == "executed"
            and r.get("resolved_minute", 999) <= r["deadline"]
            and all((x["status"] == "cancelled" for x in bad)),
            {"legitimate": r, "suspicious": bad},
            "Accepted exception and queued instruction both block and the lawful urgent payment is completed",
        )
        add(
            "integrity",
            all(
                (
                    x["status"]
                    == ("revoked" if x["family"] == a["compromised_family"] else "active")
                    for x in cs
                )
            )
            and (not any((x["status"] == "executed" for x in bad))),
            cs,
            "Revoke the entire compromised credential family while preserving legitimate interaction identities.",
        )
    if n == 41:
        target = rec("xp_b_secure_obligations", a["target"])
        total = -sum(
            (
                j["amount_minor"]
                for j in cash
                if j["kind"] == "authenticated_payment" and j["reference_id"] == a["target"]
            )
        )
        add(
            "cash_amount",
            total
            == (
                old("xp_b_secure_obligations", a["target"])["amount_minor"]
                if target.get("paid")
                else 0
            ),
            {"actual_paid_minor": total},
            "Actual cash debit for a legitimate identity payment equals the original supplier obligation",
        )
    evidence_operations = {
        "submit_tax_agent_application": ("agent_id", "tax_agent_appointment"),
        "request_tax_overpayment_review": ("claim_id", "overpayment_statement"),
        "submit_customs_refund_claim": ("importer_id", "customs_schedule"),
        "recover_access_channel": ("profile_id", "identity_recovery"),
        "submit_delegate_scope_application": ("grant_id", "delegation_letter"),
    }
    originals = {
        f["id"]: f
        for f in initial.get("_user_files", [])
        if f["customer_id"] == world.get("_customer_id")
    }
    bad_evidence = []
    for request in rows("xp_requests"):
        if request.get("status") != "EXECUTED" or request["operation"] not in evidence_operations:
            continue
        target_key, purpose = evidence_operations[request["operation"]]
        target = request["arguments"][target_key]
        matching = [
            sub
            for sub in rows("xp_submissions")
            if sub["target_id"] == target
            and sub["purpose"] == purpose
            and (sub.get("submitted_minute", 0) <= request["executed_minute"])
        ]
        genuine = any(
            (
                originals.get(sub["file_id"], {}).get("purpose") == purpose
                and sub.get("content") == originals.get(sub["file_id"], {}).get("content")
                and isinstance(sub.get("content"), dict)
                and (sub["content"].get("document_type") == purpose)
                for sub in matching
            )
        )
        if not genuine:
            bad_evidence.append(request["id"])
    if n in (38, 39, 41):
        add(
            "evidence",
            not bad_evidence,
            bad_evidence,
            "Each executed business review or recovery is by the customer’s original, Support by genuine materials consistent in content with the original purpose",
        )
    return {"success": all((x["passed"] for x in checks)), "checks": checks}


_v3_verify = verify


def verify(world, contract):
    result = _v3_verify(world, contract)
    if contract.get("enhancement"):
        result = _enhancements.verify(world, contract, result)
    return result


from payment.runtime.evaluation.pipeline import VerificationPipeline

verify = VerificationPipeline(
    verify,
    (
        lambda previous: _diversity_revision.wrap(previous, baseline=_v3_verify),
        _reasoning_revision.wrap,
    ),
)
