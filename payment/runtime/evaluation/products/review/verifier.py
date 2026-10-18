"""Independent outcome, monetary and historical predicates; no engine/solver import."""

import itertools
from payment.runtime.evaluation.products.trade.verifier import verify as inherited_verify
from payment.runtime.products.trade.records import TABLES


def verify(world, c):
    if not c.get("extension_service"):
        return inherited_verify(world, c)
    checks = []

    def check(name, ok, zh, detail=None):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, detail=detail))

    try:
        initial = c["initial_world"]
        scope = c["scope"]
        cid = c["case_id"]
        state = world.get("_closure_state") or world
        start = initial["service_cases"][cid]
        case = state["service_cases"][cid]
        k = c["kind"]
        s = case["state"]
        orig_s = start["state"]
        original = {
            x["reference"]: x for x in initial["service_records"].values() if x["case_id"] == cid
        }
        r = {x["reference"]: x for x in state["service_records"].values() if x["case_id"] == cid}
        p = original["CORRESPONDENCE"]["_response"]
        books = [x for x in state["service_bookings"].values() if x["case_id"] == cid]
        calls = world["audit"]
        closes = [a for a in calls if a["tool"] == "record_case_outcome"]
        check(
            "closure",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and (world.get("case_outcome") or {}).get("outcome") == "COMPLETED",
            "Close only once after completing the business; Early or duplicate case closure cannot be overridden by subsequent status. ",
        )
        approvals = True
        for q in state["requests"].values():
            if q.get("scope") == scope and q.get("status") == "EXECUTED":
                approvals &= any(
                    a["tool"] == "decide_request"
                    and a["arguments"]["request_id"] == q["id"]
                    and a["result"]["ok"]
                    and a["arguments"]["decision"] == "APPROVE"
                    for a in calls
                ) and q.get("approval_seq", 10**10) < q.get("executed_seq", -1)
        check(
            "approval",
            approvals,
            "Every execution request has genuine user approval with matching parameters, And approval precedes execution.. ",
        )
        exempt = {
            "audit",
            "unlocked",
            "case_outcome",
            "_closure_state",
            "_executing_approver",
            "minute",
            "requests",
            "events",
            "_event_payloads",
            *TABLES,
        }
        frame = all(state.get(t) == v for t, v in initial.items() if t not in exempt)
        for table in TABLES:
            for rid, x in initial[table].items():
                if x.get("scope") != scope or table == "service_documents":
                    frame &= state[table].get(rid) == x
        mutable = {
            "paid_minor",
            "gross_paid_minor",
            "tax_minor",
            "distributed",
            "accepted",
            "released",
            "discounted",
            "posted",
            "release_snapshot",
            "corrected_value_day",
            "adjusted",
            "adjustment_minor",
            "adjustment_period",
            "reopened",
            "reissued",
            "certificate_ref",
            "acquired",
            "disputed",
            "recovered_minor",
            "dispute_status",
            "routed",
            "routed_wallet",
            "credited_minor",
            "reimbursed_minor",
            "settled",
            "recipient",
            "resolved",
            "resolution",
            "service_entity",
            "received",
            "refreshed",
            "approved_quote",
            "executed",
            "execution_quote",
            "processed",
            "amount_minor",
            "returned_minor",
            "collected",
            "used",
            "collection_minute",
            "slot_ref",
            "collection_fee_minor",
            "repaid_minor",
            "prepaid",
            "paid_minute",
            "receipt_status",
            "receipt",
        }
        for ref, x in original.items():
            for key, value in x.items():
                if key not in mutable:
                    frame &= r.get(ref, {}).get(key) == value
            # Only a synchronized sale may change its original face amount.
            if x["kind"] != "sale_row" and "amount_minor" in x:
                frame &= r[ref]["amount_minor"] == x["amount_minor"]
        check(
            "frame",
            frame,
            "Protect other customers, business account, Original contract, historical fields and key identity relationships. ",
        )
        economic = [b for b in books if b.get("contract") == k]
        cash = start["cash_minor"]
        eur = orig_s.get("eur_minor", 0)
        fees = 0
        reserves_ok = True
        if k == "early_discount" and state["minute"] >= orig_s["later_minute"]:
            cash += orig_s["later_cash_minor"]
        for b in books:
            a = b["kind"]
            d = b.get("arguments", {})
            row = initial["service_records"].get(d.get("record_id", ""))
            fee = 0
            delta = 0
            if a == "payment":
                ob = initial["service_records"][b["record_id"]]
                if ob.get("currency") == "EUR":
                    eur -= b["amount_minor"]
                else:
                    delta -= b["amount_minor"]
            elif b.get("contract") == k:
                if k in (
                    "payroll_attachment",
                    "daily_windows",
                    "expense_classification",
                    "successor_refund",
                ) and a in ("pay", "reimburse", "refund"):
                    delta -= d["amount_minor"]
                elif k == "dividend_grossup" and a == "distribute":
                    delta -= d["gross_minor"]
                elif k == "collection_acceptance":
                    if a == "post_receipt":
                        delta += p["sight_cash_minor"]
                    elif a == "discount":
                        delta += row["face_minor"]
                        fee = row["discount_fee_minor"]
                elif k == "value_date" and a == "post_adjustment":
                    delta += d["interest_minor"]
                elif k == "certificate_revocation" and a == "replace":
                    fee = initial["service_records"][d["certificate_id"]]["fee_minor"]
                elif k == "evidence_periods" and a == "obtain":
                    fee = row["fee_minor"]
                elif k == "late_presentment":
                    if a == "dispute":
                        fee = 500
                    elif a == "pay_bill":
                        delta -= d["amount_minor"]
                elif k == "currency_refund" and a == "route":
                    n = (
                        row["historical_funding_minor"]
                        * p["refund_fraction_bp"][row["reference"]]
                        // 10000
                    )
                    if d["wallet"] == "USD":
                        delta += n
                    else:
                        eur += n
                elif k == "merger_eligibility" and a == "new_application":
                    fee = row["new_fee_minor"]
                elif k == "dual_approval" and a == "pay":
                    delta -= d["amount_minor"]
                elif k == "approval_tolerance" and a == "pay":
                    delta -= d["principal_minor"]
                    fee = p["quotes"][row["reference"]]["fee_minor"]
                elif k == "guaranteed_net":
                    if a == "send":
                        delta -= d["gross_minor"]
                        fee = initial["service_records"][d["route_id"]]["bank_fee_minor"]
                    elif a == "reconcile":
                        delta += max(
                            0,
                            s["sent_minor"]
                            - p["actual_external_minor"]
                            - orig_s["net_required_minor"],
                        )
                elif k == "billing_plan":
                    if a == "select_plan":
                        fee = row["fixed_minor"]
                    elif a == "process":
                        chosen = original[s["selected_plan"]]
                        index = sum(x["kind"] == "process" for x in economic[: economic.index(b)])
                        fee = (
                            chosen["first_fee_minor"]
                            if index < chosen["first_units"]
                            else chosen["later_fee_minor"]
                        )
                        delta -= row["amount_minor"]
                elif k == "data_scope" and a == "withdraw":
                    delta -= d["amount_minor"]
                elif k == "demurrage":
                    slot = next(
                        x
                        for x in list(original.values()) + p["extra_slots"]
                        if x.get("reference") == d["slot_ref"]
                    )
                    days = max(0, slot["minute"] - row["free_through"])
                    fee = slot["fee_minor"] + (
                        row["first_daily_minor"] + max(0, days - 1) * row["later_daily_minor"]
                        if days
                        else 0
                    )
                elif k == "early_discount" and a == "pay":
                    delta -= d["amount_minor"]
                elif k == "interest_prepayment":
                    delta -= d["amount_minor"]
                    fee = p["prepayment_amendments"][row["reference"]]["fee_minor"]
            cash += delta - fee
            fees += fee
        check(
            "cash_conservation",
            case["cash_minor"] == cash
            and s.get("eur_minor", 0) == eur
            and case["fees_minor"] == fees,
            "From the initial principal, Actual instructions and independently calculated fees rebuild balances in each currency, Do not use tool self-reports cash_delta. ",
            dict(
                expected_cash_minor=cash,
                actual_cash_minor=case["cash_minor"],
                expected_fees_minor=fees,
            ),
        )
        check(
            "reserve",
            case["cash_minor"] >= orig_s.get("reserve_minor", 0),
            "Retain customer-requested cash reserve.. ",
        )
        for x in original.values():
            if x["kind"] == "obligation":
                paid = [b for b in books if b["kind"] == "payment" and b["record_id"] == x["id"]]
                check(
                    "obligation." + x["reference"],
                    r[x["reference"]]["paid_minor"]
                    == x["amount_minor"]
                    == sum(b["amount_minor"] for b in paid)
                    and all(b["minute"] <= x["due_minute"] for b in paid),
                    "The bill is paid in full, on time, Pay in specified currency, cannot be offset by other funds. ",
                )

        def amounts(ref, actions, field="amount_minor"):
            return sum(
                b["arguments"][field]
                for b in economic
                if b["kind"] in actions and b["arguments"].get("record_id") == original[ref]["id"]
            )

        if k == "payroll_attachment":
            for x in original.values():
                if x["kind"] != "employee":
                    continue
                cap = max(0, x["gross_minor"] - x["protected_minor"])
                deduct = 0
                for claim in sorted(
                    (
                        z
                        for z in original.values()
                        if z["kind"] == "attachment" and z["employee"] == x["reference"]
                    ),
                    key=lambda z: z["priority"],
                ):
                    n = min(
                        cap, max(0, claim["face_minor"] - p["stays"].get(claim["reference"], 0))
                    )
                    cap -= n
                    deduct += n
                    check(
                        "attachment." + claim["reference"],
                        r[claim["reference"]]["paid_minor"]
                        == n
                        == amounts(claim["reference"], {"pay"}),
                        "Calculate the creditor’s actual payment based on the stayed execution amount and claim priority. ",
                    )
                n = x["gross_minor"] - deduct
                check(
                    "net_wage." + x["reference"],
                    r[x["reference"]]["paid_minor"] == n == amounts(x["reference"], {"pay"}),
                    "Net salary consistent with protected income and actual garnishment. ",
                )
        elif k == "dividend_grossup":
            total = 0
            for x in original.values():
                if x["kind"] != "shareholder":
                    continue
                actual = r[x["reference"]]
                g = actual["gross_paid_minor"]
                rate = (
                    x["treaty_bp"]
                    if p["residence_corrections"][x["reference"]]["effective_day"]
                    <= orig_s["payment_day"]
                    else x["default_bp"]
                )
                tax = (g * rate + 9999) // 10000
                total += tax
                check(
                    "net_and_tax." + x["reference"],
                    g == (x["net_promised_minor"] * 10000 + 9999 - rate) // (10000 - rate)
                    and g - tax == x["net_promised_minor"]
                    and actual["tax_minor"] == tax
                    and g == amounts(x["reference"], {"distribute"}, "gross_minor"),
                    "Independently calculate tax amount and committed net dividend based on effective resident status. ",
                )
            check(
                "tax_remitted",
                s["tax_held_minor"] == 0
                and s["tax_paid_minor"]
                == total
                == sum(
                    b["arguments"]["amount_minor"] for b in economic if b["kind"] == "remit_tax"
                ),
                "All withheld payments are actually paid to the tax authority., Balance is zero. ",
            )
        elif k == "collection_acceptance":
            for x in r.values():
                if x["kind"] == "collection_item":
                    check(
                        "document_right." + x["reference"],
                        x["released"]
                        and x["paid_minor"] == x["face_minor"]
                        and (
                            x["release_snapshot"]["paid_minor"] == x["face_minor"]
                            if x["terms"] == "DP"
                            else x["release_snapshot"]["accepted"]
                        ),
                        "Check payment or acceptance conditions at the time of document release.; Post-event supplementary payment does not eliminate early release of documents. ",
                    )
            check(
                "discounted_bill",
                all(
                    x["discounted"] and x["accepted"]
                    for x in r.values()
                    if x["kind"] == "collection_item" and x["terms"] == "DA"
                ),
                "Acceptance bill is discounted once, Obtain actual freight payment funds. ",
            )
        elif k == "daily_windows":
            for ref, x in original.items():
                if x["kind"] != "window_payment":
                    continue
                ps = [b for b in economic if b["arguments"]["record_id"] == x["id"]]
                check(
                    "window_obligation." + ref,
                    r[ref]["paid_minor"]
                    == x["amount_minor"]
                    == sum(b["arguments"]["amount_minor"] for b in ps)
                    and all(b["minute"] <= x["due_minute"] for b in ps),
                    "Independently verify cumulative payments and deadlines by payee. ",
                )
            for boundary in (False, True):
                used = (0 if boundary else p["sender_used_minor"]) + sum(
                    b["arguments"]["amount_minor"]
                    for b in economic
                    if (b["minute"] >= orig_s["sender_reset_minute"]) == boundary
                )
                check(
                    "sender_capacity." + str(boundary),
                    used <= orig_s["sender_limit_minor"],
                    "independently recalculate shared sending window usage. ",
                )
            for ref, x in original.items():
                if x["kind"] != "window_payment":
                    continue
                for after in (False, True):
                    used = (0 if after else p["recipient_used_minor"][ref]) + sum(
                        b["arguments"]["amount_minor"]
                        for b in economic
                        if b["arguments"]["record_id"] == x["id"]
                        and (b["minute"] >= x["recipient_reset_minute"]) == after
                    )
                    check(
                        "recipient_capacity." + ref + "." + str(after),
                        used <= x["recipient_limit_minor"],
                        "Independently recalculate this payee’s actual usage in the windows before and after reset. ",
                    )
        elif k == "value_date":
            x = original["POSTED-CREDIT"]
            n = (
                x["amount_minor"]
                * x["annual_bp"]
                * (x["value_day"] - p["bank_value_day"])
                // (10000 * p["interest_basis_days"])
            )
            y = r["POSTED-CREDIT"]
            check(
                "dated_interest",
                y.get("corrected_value_day") == p["bank_value_day"]
                and y.get("adjustment_minor") == n
                and y.get("adjustment_period") == orig_s["current_period"],
                "Correct value date and interest accrual period, Retain the closed original account and use the current adjustment period.. ",
            )
        elif k == "certificate_revocation":
            for ref, x in original.items():
                if x["kind"] == "evidence_reliance":
                    affected = x["business_day"] >= (
                        original["OLD-CERT"]["valid_from_day"]
                        if p["revocation_mode"] == "ex_tunc"
                        else p["effective_day"]
                    )
                    y = r[ref]
                    check(
                        "temporal_validity." + ref,
                        y["accepted"]
                        and y["reopened"] == affected
                        and y["reissued"] == affected
                        and y["certificate_ref"] == ("NEW-CERT" if affected else "OLD-CERT"),
                        "Determine whether to reopen based on the effective time of revocation, Historically valid items continue to be retained. ",
                    )
            check(
                "paperwork_budget",
                fees <= orig_s["fee_cap_minor"],
                "Prove reissue fee complies with budget.. ",
            )
        elif k == "evidence_periods":
            xs = [state["service_records"][rid] for rid in s.get("submitted_ids", [])]
            coverage = all(
                any(x["entity"] == span["entity"] and x["start"] <= day <= x["end"] for x in xs)
                for span in p["entity_periods"]
                for day in range(span["start"], span["end"] + 1)
            )
            subjects = not p.get("strict_subject_period") or all(
                any(
                    x["entity"] == span["entity"]
                    and span["start"] <= x["start"] <= x["end"] <= span["end"]
                    for span in p["entity_periods"]
                )
                for x in xs
            )
            check(
                "evidence_coverage",
                coverage and subjects and s["accepted"] and all(x["acquired"] for x in xs),
                "Independently check each day and entity coverage, And verify the review has been accepted. ",
            )
            check(
                "document_budget",
                fees <= orig_s["fee_cap_minor"],
                "Costs paid to obtain evidence are within the cap. ",
            )
        elif k == "late_presentment":
            recovered = 0
            for ref, proof in p["merchant_proofs"].items():
                x = r[ref]
                n = (
                    0
                    if proof["offline_consent"]
                    else max(
                        0, original[ref]["amount_minor"] - p["posted_credits_minor"].get(ref, 0)
                    )
                )
                recovered += n
                check(
                    "presentment." + ref,
                    x["recovered_minor"] == n and x["disputed"] == (not proof["offline_consent"]),
                    "Restore amounts that were genuinely unauthorized and not refunded, Do not dispute lawful offline clearing. ",
                )
            check(
                "actual_card_bill",
                s["card_due_minor"] == orig_s["card_due_minor"] - recovered == s["card_paid_minor"],
                "Card statement payments adjusted by actual refunds.. ",
            )
        elif k == "currency_refund":
            for ref, x in original.items():
                if x["kind"] == "refund":
                    check(
                        "funding_wallet." + ref,
                        r[ref]["routed"]
                        and r[ref]["routed_wallet"] == x["funding_currency"]
                        and r[ref]["credited_minor"]
                        == x["historical_funding_minor"] * p["refund_fraction_bp"][ref] // 10000,
                        "Restore historical entitlements based on original funding wallets and actual refund proportions. ",
                    )
        elif k == "expense_classification":
            for ref, x in original.items():
                if x["kind"] != "expense_claimant":
                    continue
                eligible = {}
                for item in original.values():
                    if (
                        item["kind"] == "expense_item"
                        and item["employee"] == ref
                        and item["reference"] not in p["void_items"]
                    ):
                        purpose = item["purpose"]
                        eligible[purpose] = (
                            eligible.get(purpose, 0)
                            + item["amount_minor"] * p["eur_usd_num"] // p["eur_usd_den"]
                        )
                n = max(
                    0,
                    min(eligible.get("business_meal", 0), p["meal_cap_usd_minor"])
                    + min(eligible.get("work_space", 0), p["workspace_cap_usd_minor"])
                    - x["advance_minor"],
                )
                check(
                    "reimbursement." + ref,
                    r[ref]["reimbursed_minor"] == n == amounts(ref, {"reimburse"}),
                    "By itemized use, Currency, Independently account for cap and historical advances in personal reimbursement. ",
                )
        elif k == "successor_refund":
            for ref, x in original.items():
                if x["kind"] == "refund_claim":
                    owners = [
                        z["new"]
                        for z in p["succession"]
                        if z["old"] == x["origin_entity"]
                        and z["from_day"] <= p["payment_day"]
                        and ref in z["assets"]
                    ]
                    owner = owners[-1] if owners else x["origin_entity"]
                    check(
                        "legal_refund." + ref,
                        r[ref]["recipient"] == owner
                        and r[ref]["paid_minor"]
                        == x["amount_minor"] - x["prior_refund_minor"]
                        == amounts(ref, {"refund"}),
                        "Determine creditors according to effective succession and asset list, Refund deducts historical paid amounts. ",
                    )
        elif k == "merger_eligibility":
            for ref, event in p["events"].items():
                check(
                    "continuity." + ref,
                    r[ref]["resolution"]
                    == ("retain" if event["type"] == "SHARE_PURCHASE" else "new_application")
                    and r[ref]["service_entity"] == event["successor"]
                    and r[ref]["active"],
                    "Choose retention or reprocessing depending on whether the legal entity continues, And maintain correct entity collection. ",
                )
        elif k == "dual_approval":
            valid = {
                x["person"]
                for ref, x in r.items()
                if x["kind"] == "signature"
                and x["received"]
                and ref in p["actual_signed_refs"]
                and ref not in p["withdrawn_refs"]
                and x["version"] == orig_s["payment_version"]
                and x["payment_scope"]
            }
            check(
                "independent_signers",
                len(valid) >= 2,
                "Deduplicate signatures by natural person identity, Check version, Authority and actual signing. ",
            )
            check(
                "signed_payment",
                s["paid_minor"] == orig_s["payment_minor"] and s.get("beneficiary") == "SUPPLIER",
                "Pay the designated supplier in full., Do not treat another payee as equivalent. ",
            )
        elif k == "approval_tolerance":
            for ref, q in p["quotes"].items():
                x = r[ref]
                old = original[ref]
                ok = (
                    q["fee_minor"] <= old["fee_cap_minor"]
                    and q["beneficiary"] == old["beneficiary"]
                    and q["currency"] == old["currency"]
                )
                check(
                    "quote_binding." + ref,
                    x["paid_minor"] == old["principal_minor"]
                    and x.get("execution_quote") == q
                    and (ok or x.get("approved_quote") == q),
                    "Verify actual principal, Current quote bound to allowed old approval or new approval. ",
                )
        elif k == "guaranteed_net":
            route = original.get(s.get("route_ref"), {})
            upper = p["fee_revision"].get(s.get("route_ref"), {}).get("external_max_minor", 10**20)
            check(
                "commitment_solvency",
                route.get("guarantee") and s["sent_minor"] >= orig_s["net_required_minor"] + upper,
                "Prove net commitment by fee upper bound at submission time., Cannot look only at actual low fee. ",
            )
            check(
                "net_reconciled",
                s.get("receipt_ready")
                and s["reconciled"]
                and s["received_minor"] == orig_s["net_required_minor"],
                "Actual credit receipt has arrived, Fees have been written off and remaining balance returned to wallet. ",
            )
        elif k == "billing_plan":
            jobs = [x for x in original.values() if x["kind"] == "scheduled_job"]
            n = sum(x["reference"] not in p["cancelled_refs"] for x in jobs)
            costs = [
                x["fixed_minor"]
                + min(n, x["first_units"]) * x["first_fee_minor"]
                + max(0, n - x["first_units"]) * x["later_fee_minor"]
                for x in original.values()
                if x["kind"] == "billing_offer"
            ]
            for x in jobs:
                check(
                    "job." + x["reference"],
                    r[x["reference"]]["paid_minor"]
                    == (0 if x["reference"] in p["cancelled_refs"] else x["amount_minor"]),
                    "Only pay for currently still valid business, Canceled items remain unpaid. ",
                )
            check(
                "minimum_future_cost",
                fees == min(costs),
                "Independently compare new fees for the remaining business, Exclude already incurred sunk costs. ",
                dict(feasible_costs=costs),
            )
        elif k == "data_scope":
            expected = {
                ref: [x["amount_minor"], x["returned_minor"]]
                for ref, x in original.items()
                if x["kind"] == "sale_row"
            }
            for e in p["delta"]:
                if e["cursor"] > orig_s["cursor"]:
                    expected[e["reference"]][0 if e["kind"] == "order" else 1] += e["amount_minor"]
            check(
                "delta_integrity",
                all(
                    [r[ref]["amount_minor"], r[ref]["returned_minor"]] == xs
                    for ref, xs in expected.items()
                )
                and s["cursor"] == max(e["cursor"] for e in p["delta"]),
                "Incremental data precisely merged once, Retain historical orders and link actual returns. ",
            )
            check(
                "allowance_and_withdrawal",
                s["withdrawn_minor"] == p["withdrawal_minor"]
                and s["quota_minor"]
                == sum(v[0] - v[1] for v in expected.values()) - s["withdrawn_minor"],
                "Independently calculate credit limit from orders and returns, And complete requested withdrawal. ",
            )
        elif k == "demurrage":
            cargo = [x for x in original.values() if x["kind"] == "container"]
            slots = [x for x in original.values() if x["kind"] == "appointment"] + p["extra_slots"]
            costs = []
            for choices in itertools.product(slots, repeat=len(cargo)):
                if any(
                    sum(z["reference"] == slot["reference"] for z in choices) > slot["capacity"]
                    for slot in slots
                ):
                    continue
                if any(z["minute"] > x["due_minute"] for x, z in zip(cargo, choices)):
                    continue
                cost = 0
                for x, z in zip(cargo, choices):
                    late = max(0, z["minute"] - x["free_through"])
                    cost += z["fee_minor"] + (
                        x["first_daily_minor"] + (late - 1) * x["later_daily_minor"] if late else 0
                    )
                costs.append(cost)
            check(
                "optimal_collection",
                all(
                    r[x["reference"]]["collected"]
                    and r[x["reference"]]["collection_minute"] <= x["due_minute"]
                    for x in cargo
                )
                and fees == min(costs),
                "Fully enumerate current finite reservation combinations, Verify shared capacity, time limit and minimum total cost. ",
                dict(
                    assignments_considered=len(slots) ** len(cargo),
                    feasible_assignments=len(costs),
                    minimum_minor=min(costs),
                ),
            )
        elif k == "early_discount":
            invoices = [x for x in original.values() if x["kind"] == "discount_invoice"]
            options = []
            for subset in itertools.product((False, True), repeat=len(invoices)):
                early = late = 0
                for x, take in zip(invoices, subset):
                    face = x["face_minor"] - p["credits"].get(x["reference"], 0)
                    if take:
                        early += face - face * x["discount_bp"] // 10000
                    else:
                        late += face
                if (
                    early <= start["cash_minor"] - orig_s["reserve_minor"]
                    and early + late
                    <= start["cash_minor"] + orig_s["later_cash_minor"] - orig_s["reserve_minor"]
                ):
                    options.append(early + late)
            total = 0
            for x in invoices:
                y = r[x["reference"]]
                face = x["face_minor"] - p["credits"].get(x["reference"], 0)
                expected = (
                    face - face * x["discount_bp"] // 10000
                    if y.get("paid_minute", 10**10) <= x["discount_until"]
                    else face
                )
                total += y["paid_minor"]
                check(
                    "invoice_settlement." + x["reference"],
                    y["settled"]
                    and y["paid_minor"] == expected
                    and y["paid_minute"] <= x["due_minute"],
                    "Determine payable amount and discount eligibility by credit note and actual payment time. ",
                )
            early_used = sum(
                b["arguments"]["amount_minor"]
                for b in economic
                if b["minute"] < orig_s["later_minute"]
            )
            check(
                "cash_timing",
                early_used <= start["cash_minor"] - orig_s["reserve_minor"],
                "Operating funds must also be retained before future incoming payments occur. ",
            )
            check(
                "minimum_invoice_total",
                total == min(options),
                "Fully compare early payment subset, Verify total payment for all invoices is minimum.. ",
                dict(subsets=2 ** len(invoices), minimum_minor=min(options)),
            )
        elif k == "interest_prepayment":
            loans = [x for x in original.values() if x["kind"] == "loan"]
            q = orig_s["allocation_quantum_minor"]
            available = (
                start["cash_minor"]
                - orig_s["reserve_minor"]
                - original["EQUIPMENT"]["amount_minor"]
            )
            scores = []
            ranges = [
                [0] + list(range(x["min_prepay_minor"], x["principal_minor"] + 1, q)) for x in loans
            ]
            for allocation in itertools.product(*ranges):
                f = sum(
                    p["prepayment_amendments"][x["reference"]]["fee_minor"]
                    for x, n in zip(loans, allocation)
                    if n
                )
                if sum(allocation) + f <= available:
                    scores.append(
                        f
                        + sum(
                            ((x["principal_minor"] - n) * x["period_interest_bp"] + 9999) // 10000
                            for x, n in zip(loans, allocation)
                        )
                    )
            actual = fees + sum(
                (
                    (x["principal_minor"] - r[x["reference"]]["repaid_minor"])
                    * x["period_interest_bp"]
                    + 9999
                )
                // 10000
                for x in loans
            )
            check(
                "minimum_finance_cost",
                actual == min(scores),
                "Exhaustively enumerate within the public minimum amount and step size range, Compare remaining interest plus new fees. ",
                dict(
                    feasible_allocations=len(scores), actual_minor=actual, minimum_minor=min(scores)
                ),
            )
        else:
            check(
                "supported_contract",
                False,
                "Unknown business specification, Default approval prohibited. ",
            )
        if k == "successor_refund":
            claims = [x for x in original.values() if x["kind"] == "refund_claim"]
            recipients = [x for x in original.values() if x["kind"] == "legal_recipient"]
            check(
                "two_independent_refund_claims",
                len(claims) == 2
                and {x["origin_entity"] for x in claims} == {"CEDAR-OLD", "ASH"}
                and len({x["id"] for x in claims + recipients}) == len(claims) + len(recipients),
                "both refunds have independent records, Not overwritten by a same-name payee. ",
            )
        for name, passed, zh in ledger_constraints(world, state, c, original, r, books):
            check(name, passed, zh)
    except (KeyError, TypeError, ValueError, IndexError, StopIteration) as exc:
        check(
            "evaluator_integrity",
            False,
            "Acceptance fails when status is missing or malformed. ",
            str(exc),
        )
    return dict(
        success=bool(checks) and all(x["passed"] for x in checks),
        checks=checks,
        failed=[x["name"] for x in checks if not x["passed"]],
    )


def ledger_constraints(world, state, c, original, records, books):
    """Tie mutable completion fields to real requests, receipts and prior events."""
    cid = c["case_id"]
    k = c["kind"]
    s = state["service_cases"][cid]["state"]
    source = original["CORRESPONDENCE"]["_response"]
    audit = world["audit"]
    out = []

    def check(name, ok, zh):
        out.append((name, bool(ok), zh))

    def selected(action, ref=None):
        return [
            b
            for b in books
            if b.get("contract") == k
            and b["kind"] == action
            and (ref is None or b["arguments"].get("record_id") == original[ref]["id"])
        ]

    def done(kind, target):
        return any(
            e["kind"] == kind and e["target_id"] == target and e["status"] == "COMPLETED"
            for e in state["events"].values()
        )

    for b in books:
        calls = [
            a
            for a in audit
            if a["tool"] == "execute_request" and a["result"]["ok"] and a["result"].get("data") == b
        ]
        requests = [
            q
            for q in state["requests"].values()
            if q.get("status") == "EXECUTED" and q.get("receipt") == b
        ]
        check(
            "ledger_receipt." + b["id"],
            bool(calls and requests),
            "This business journal entry has a genuine execution receipt and matching request, Cannot only change the final status. ",
        )
    if k == "collection_acceptance":
        for ref, x in original.items():
            if x["kind"] != "collection_item":
                continue
            release = selected("release_documents", ref)
            prior = selected("post_receipt" if x["terms"] == "DP" else "register_acceptance", ref)
            ok = (
                len(release) == 1
                and bool(prior)
                and any(books.index(x) < books.index(release[0]) for x in prior)
            )
            ok &= x["terms"] == "DP" or len(selected("discount", ref)) == 1
            check(
                "historical_delivery." + ref,
                ok,
                "Check in actual execution order that payment or acceptance occurred before release, And verify the discount entry. ",
            )
    elif k == "value_date":
        bs = selected("post_adjustment", "POSTED-CREDIT")
        r = records["POSTED-CREDIT"]
        check(
            "adjustment_event",
            len(bs) == 1
            and bs[0]["arguments"]["interest_minor"] == r.get("adjustment_minor")
            and bs[0]["arguments"]["value_day"] == r.get("corrected_value_day"),
            "Date and interest adjustments must come from executed current-period journal entries. ",
        )
    elif k == "certificate_revocation":
        for ref, x in original.items():
            if x["kind"] != "evidence_reliance":
                continue
            row = records[ref]
            rs = selected("reopen", ref)
            ps = selected("replace", ref)
            ok = row["reopened"] == bool(rs) and row["reissued"] == bool(ps)
            if ps:
                cert = next(
                    y for y in original.values() if y["id"] == ps[-1]["arguments"]["certificate_id"]
                )
                ok &= (
                    row["certificate_ref"] == cert["reference"]
                    and cert.get("covers_from_day", 10**8)
                    <= x["business_day"]
                    <= cert.get("covers_through_day", -1)
                    and books.index(rs[0]) < books.index(ps[0])
                )
            check(
                "replacement_event." + ref,
                ok,
                "Reopening and reissuing both have actual events, Substitute proof covers the original business date. ",
            )
    elif k == "evidence_periods":
        bs = selected("submit")
        review = state["service_reviews"].get(s.get("current_review_id"), {})
        check(
            "review_event",
            bool(bs)
            and bs[-1]["arguments"]["record_ids"]
            == s.get("submitted_ids")
            == review.get("document_ids")
            and bs[-1].get("receipt", {}).get("review_id") == review.get("id")
            and review.get("status") == "ACCEPTED"
            and done("extension_evidence_review", review.get("id")),
            "The current submitted version has a corresponding material snapshot and has actually passed review, Previous review cannot replace this result. ",
        )
        for ref, x in original.items():
            if x["kind"] == "statement_offer":
                check(
                    "document_acquisition." + ref,
                    records[ref]["acquired"] == (x["acquired"] or bool(selected("obtain", ref))),
                    "Fee documents have actual acquisition records, Free old documents allowed to be reused. ",
                )
    elif k == "late_presentment":
        for ref, x in original.items():
            if x["kind"] == "card_presentment":
                check(
                    "dispute_receipt." + ref,
                    records[ref]["disputed"] == bool(selected("dispute", ref))
                    and (not records[ref]["disputed"] or done("extension_card_dispute", x["id"])),
                    "Dispute flags correspond to actual submission and actual decision events.. ",
                )
        check(
            "card_payment_receipt",
            s["card_paid_minor"]
            == sum(b["arguments"]["amount_minor"] for b in selected("pay_bill")),
            "Card statement payment status matches actual payment entries. ",
        )
    elif k == "currency_refund":
        for ref, x in original.items():
            if x["kind"] == "refund":
                bs = selected("route", ref)
                check(
                    "refund_receipt." + ref,
                    len(bs) == 1 and records[ref]["routed_wallet"] == bs[0]["arguments"]["wallet"],
                    "The refund target wallet is determined by the actual irrevocable routing. ",
                )
    elif k == "successor_refund":
        for ref, x in original.items():
            if x["kind"] == "refund_claim":
                bs = selected("refund", ref)
                check(
                    "refund_recipient_receipt." + ref,
                    len(bs) == 1
                    and any(
                        y["id"] == bs[0]["arguments"]["recipient_id"]
                        and y["reference"] == records[ref]["recipient"]
                        for y in original.values()
                    ),
                    "Legal payee and actual refund recipient are consistent.. ",
                )
    elif k == "merger_eligibility":
        for ref, x in original.items():
            if x["kind"] == "collection_service":
                bs = selected("retain", ref) + selected("new_application", ref)
                check(
                    "service_resolution_receipt." + ref,
                    len(bs) == 1
                    and bs[0]["kind"] == records[ref]["resolution"]
                    and bs[0]["arguments"]["entity"] == records[ref]["service_entity"],
                    "Service resolution status corresponds to the actually retained or newly opened entity and request. ",
                )
    elif k == "dual_approval":
        bs = selected("pay")
        ok = (
            bool(bs)
            and s["paid_minor"] == sum(b["arguments"]["amount_minor"] for b in bs)
            and all(b["arguments"]["beneficiary"] == "SUPPLIER" for b in bs)
        )
        for b in bs:
            people = {
                original[ref]["person"]
                for ref in original
                if original[ref]["kind"] == "signature"
                and any(
                    books.index(sig) < books.index(b) for sig in selected("register_signature", ref)
                )
                and ref in source["actual_signed_refs"]
                and ref not in source["withdrawn_refs"]
                and original[ref]["version"] == s["payment_version"]
                and original[ref]["payment_scope"]
            }
            ok &= len(people) >= 2
        check(
            "payment_after_independent_signatures",
            ok,
            "Before each actual payment, valid signatures from two different natural persons already exist. ",
        )
    elif k == "approval_tolerance":
        for ref, q in source["quotes"].items():
            bs = selected("pay", ref)
            old = original[ref]
            valid = (
                q["fee_minor"] <= old["fee_cap_minor"]
                and q["beneficiary"] == old["beneficiary"]
                and q["currency"] == old["currency"]
            )
            ok = (
                len(bs) == 1 and records[ref]["paid_minor"] == bs[0]["arguments"]["principal_minor"]
            )
            if not valid and bs:
                ok &= any(
                    books.index(x) < books.index(bs[0]) for x in selected("refresh_approval", ref)
                )
            check(
                "authorization_before_payment." + ref,
                ok,
                "When authorization needs updating, Real update occurs before actual payment. ",
            )
    elif k == "guaranteed_net":
        bs = selected("send")
        rs = selected("reconcile")
        check(
            "guarantee_receipts",
            len(bs) == 1
            and len(rs) == 1
            and bs[0]["arguments"]["gross_minor"] == s["sent_minor"]
            and done("extension_remit_receipt", cid)
            and books.index(bs[0]) < books.index(rs[0]),
            "Send, Actual fee receipts and write-offs both genuinely occur, Fields cannot replace events. ",
        )
    elif k == "billing_plan":
        bs = selected("select_plan")
        check(
            "billing_selection_receipt",
            len(bs) == 1 and original[s["selected_plan"]]["id"] == bs[0]["arguments"]["record_id"],
            "package selection corresponds to the actual charge request. ",
        )
        for ref, x in original.items():
            if x["kind"] == "scheduled_job":
                check(
                    "job_receipt." + ref,
                    records[ref]["paid_minor"] == len(selected("process", ref)) * x["amount_minor"],
                    "Each completed item has a corresponding actual payment entry. ",
                )
    elif k == "data_scope":
        check(
            "sync_and_withdrawal_receipts",
            bool(selected("sync"))
            and s["withdrawn_minor"]
            == sum(b["arguments"]["amount_minor"] for b in selected("withdraw")),
            "Incremental sync and withdrawals both have actual execution records.. ",
        )
        granted = set(c["initial_world"]["service_cases"][cid]["state"]["scopes"]) | {
            b["arguments"]["scope"] for b in selected("grant_scope")
        }
        check(
            "scope_grant_receipts",
            set(s["scopes"]) == granted,
            "New data scopes must have genuine authorization execution records. ",
        )
    elif k == "demurrage":
        for ref, x in original.items():
            if x["kind"] == "container":
                bs = selected("book_collection", ref)
                check(
                    "collection_order." + ref,
                    len(bs) == 1 and bs[0]["arguments"]["slot_ref"] == records[ref].get("slot_ref"),
                    "Appointment status is consistent with the actually locked container pickup order. ",
                )
    elif k == "early_discount":
        for ref, x in original.items():
            if x["kind"] == "discount_invoice":
                bs = selected("pay", ref)
                check(
                    "invoice_payment_event." + ref,
                    len(bs) == 1
                    and bs[0]["arguments"]["amount_minor"] == records[ref]["paid_minor"]
                    and bs[0]["minute"] == records[ref].get("paid_minute"),
                    "The invoice settlement amount and discount application timing come from actual payment events. ",
                )
    elif k == "interest_prepayment":
        for ref, x in original.items():
            if x["kind"] == "loan":
                check(
                    "loan_principal_receipt." + ref,
                    records[ref]["repaid_minor"]
                    == sum(b["arguments"]["amount_minor"] for b in selected("prepay", ref)),
                    "Loan principal reduction must be supported by actual repayment, Cannot change only the loan balance. ",
                )
    return out
