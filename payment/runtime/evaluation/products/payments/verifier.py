"""Independent state/event predicates. Does not import transitions or witnesses."""

from fractions import Fraction
from collections import defaultdict
from copy import deepcopy

LABELS = {
    "operation_provenance": "Actual economic payment can be traced to a genuinely executed request, Do not accept fabricated final-state payment records. ",
    "closure": "Submit only once COMPLETED, And the business objective has already been achieved when the case is closed; Subsequent backfill actions cannot repair. ",
    "approval": "Every executed request has prior real user approval, Parameters match the application. ",
    "frame": "Unrelated accounts are not modified, Historical portfolio and other product business; Legal queries and failure receipts do not deduct points. ",
    "cash": "Calculate opening balance by currency, Actual transactions and bank events, final-state balances must be equal. ",
    "intent": "Allocation uses final payment instruction., All invoice principal is exactly written off and bank fees are independently bridged. ",
    "unique_payment": "Each valid batch item is credited exactly once, Zero payment for voided items, Collection version is correct.. ",
    "tax_posting": "Tax bureau actual received allocation is consistent with the current certificate designation; Requests within the bank window must also correct the bank memo.. ",
    "evidence_set": "Independently aggregate shareholding and consolidate controllers; Actually approved evidence set covers current version at audit time. ",
    "collection_ready": "The two specified sites become able to receive payments after real testing, Bind in accordance with actual sender and product.. ",
    "institution_authority": "Actual payer., Executor., The authorization version in effect at the time, Amount and term fully match. ",
    "component_loss": "The final compensation for each loss item exactly equals its actual loss., No cross-item offset or duplicate compensation. ",
    "lot_eligibility": "Before actual payment, all restrictions and rights on each fund have been applied and released, And the purpose is eligible. ",
    "exit_obligations": "Old bill, Refund and retained service next-period charge truly completed; Only retire agreed services and accounts. ",
    "net_delivery": "Actual net receipts in both currencies are accurate, Segment-by-segment rounding is correct, Retention amount and fee cap are simultaneously satisfied. ",
}

LABELS.update(
    tax_cutoffs="Independently reconstruct the effective tax allocation as of each cutoff time, Full amount and no overdue fees. ",
    collection_launches="Each site completes the actual collection test before the agreed go-live deadline. ",
    issuance_budget="Actual total account opening fees do not exceed the customer’s total budget. ",
    mandate_capacity="Independently accumulate actual capacity consumption according to the institution-authorized valid version, and verify the purpose. ",
    filing_budget="Actual total filing fee did not exceed budget. ",
    registered_scope="The final declared item falls within the coverage actually registered and fixed before the deadline. ",
)


def verify(world, c):
    checks = []

    def check(name, condition, detail=None):
        checks.append(
            dict(
                name=name,
                passed=bool(condition),
                description_zh=LABELS.get(name, name),
                detail=detail,
            )
        )

    try:
        i = c["initial_world"]
        a = world.get("audit", [])
        w = world.get("_closure_state") or world
        closes = [x for x in a if x["tool"] == "record_case_outcome"]
        check(
            "closure",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED"
            and bool(world.get("case_outcome")),
        )
        for r in w["requests"].values():
            if r["status"] != "EXECUTED":
                continue
            approve = [
                x
                for x in a
                if x["tool"] == "decide_request"
                and x["actor"] == "user"
                and x["arguments"] == dict(request_id=r["id"], decision="APPROVE")
                and x["result"]["ok"]
                and x["seq"] < r["executed_seq"]
            ]
            prepared = [
                x
                for x in a
                if x["tool"] == "call_discoverable_agent_tool"
                and x["result"]["ok"]
                and x["result"].get("data", {}).get("id") == r["id"]
                and x["arguments"]["capability_name"] == r["operation"]
                and x["arguments"]["arguments"] == r["arguments"]
            ]
            check("approval", bool(approve and prepared), r["id"])
        from payment.runtime.products.payments.specification import EXTRA_TABLES

        exempt = set(EXTRA_TABLES) | {
            "submissions",
            "requests",
            "events",
            "operators",
            "files",
            "_event_payloads",
            "_closure_state",
            "case_outcome",
        }
        check(
            "frame",
            all(w.get(t) == v for t, v in i.items() if isinstance(v, dict) and t not in exempt)
            and all(
                w[t].get(id) == r
                for t in EXTRA_TABLES
                for id, r in i[t].items()
                if r.get("scope") == "ARCHIVE"
            ),
        )
        # Reconstruct cash from the economic objects, not the execution journal.
        balances = {id: deepcopy(r["wallet"]) for id, r in i["desk_accounts"].items()}
        for id, r in w["desk_accounts"].items():
            balances.setdefault(id, {k: 0 for k in r["wallet"]})

        def delta(id, cur, n):
            balances[id][cur] = balances[id].get(cur, 0) + n

        for x in w["submission_attempts"].values():
            if x["id"] not in i["submission_attempts"]:
                item = w["payment_batch_items"][x["item_id"]]
                delta(item["account_id"], item["currency"], -x["amount_minor"])
        for p in w["desk_payments"].values():
            if p["kind"] == "TREASURY":
                delta(p["account_id"], p["currency"], -p["amount_minor"])
            elif p["kind"] == "CURRENCY":
                delta(p["account_id"], p["currency"], -p["amount_minor"] - p["fee_minor"])
        for q in w["fx_quotes"].values():
            if q["status"] == "EXECUTED":
                delta(q["account_id"], q["source_currency"], -q["source_minor"])
                delta(q["account_id"], q["fee_currency"], -q["fee_minor"])
                if any(
                    t["quote_id"] == q["id"] and t["status"] == "SETTLED"
                    for t in w["fx_trades"].values()
                ):
                    delta(q["account_id"], q["target_currency"], q["target_minor"])
        for r in w["requests"].values():
            if r["status"] == "EXECUTED" and r["operation"] == "propose_account_sweep":
                v = r["arguments"]
                cur = w["desk_accounts"][v["source_account_id"]]["currency"]
                delta(v["source_account_id"], cur, -v["amount_minor"])
                delta(v["destination_account_id"], cur, v["amount_minor"])
        for b in w["billing_instances"].values():
            if b["status"] == "PAID":
                delta(b["account_id"], b["currency"], -b["amount_minor"])
            if b["status"] == "REFUNDED":
                delta(b["account_id"], b["currency"], b["amount_minor"])
        for d in w["reserve_disbursements"].values():
            delta(d["reserve_account_id"], "USD", -d["debit_minor"])
            delta(d["destination_account_id"], "USD", d["net_minor"])
        for r in w["requests"].values():
            if r["status"] == "EXECUTED" and r["operation"] == "propose_bank_message_amendment":
                transfer = i["tax_transfers"][r["arguments"]["transfer_id"]]
                if transfer.get("amendment_fee_minor"):
                    delta(
                        transfer["amendment_fee_account_id"],
                        "USD",
                        -transfer["amendment_fee_minor"],
                    )
        review_fees = []
        for request in w["requests"].values():
            if (
                request["status"] == "EXECUTED"
                and request["operation"] == "propose_onboarding_submission"
            ):
                bundle = w["evidence_bundles"][request["arguments"]["bundle_id"]]
                app = i["onboarding_applications"][bundle["application_id"]]
                fee = app.get("submission_fee_minor", 0)
                review_fees.append(fee)
                if fee:
                    delta(app["billing_account_id"], "USD", -fee)
                check("review_fee_receipt", bundle.get("charged_fee_minor", 0) == fee)
        issuance_fees = 0
        filing_fees = 0
        for request in w["requests"].values():
            if request["status"] != "EXECUTED":
                continue
            v = request["arguments"]
            if request["operation"] == "propose_desk_account_opening":
                profile = i["rail_profiles"][v["profile_id"]]
                fee = profile.get("issuance_fee_minor", 0)
                issuance_fees += fee
                if fee:
                    delta(profile["billing_account_id"], "USD", -fee)
            if request["operation"] == "propose_card_dispute":
                case = i["card_loss_cases"][v["case_id"]]
                fee = case.get("filing_fee_minor", 0)
                filing_fees += fee
                if fee:
                    delta(case["billing_account_id"], "USD", -fee)
        cutoff_positions = {}
        for pid, period in i["tax_periods"].items():
            if "filing_cutoff_minute" not in period:
                continue
            deadline = period["filing_cutoff_minute"]
            latest = {}
            for posting in w["tax_allocations"].values():
                if "posted_minute" not in posting or posting["posted_minute"] > deadline:
                    continue
                tid = posting["transfer_id"]
                if tid not in latest or (posting["posted_minute"], posting["id"]) > (
                    latest[tid]["posted_minute"],
                    latest[tid]["id"],
                ):
                    latest[tid] = posting
            paid = sum(x["allocations"].get(pid, 0) for x in latest.values())
            cutoff_positions[pid] = paid
            if w["minute"] >= deadline and paid < period["amount_minor"]:
                delta(period["fee_account_id"], "USD", -period["late_fee_minor"])
        check(
            "cash",
            all(
                r["wallet"] == balances[id] and all(n >= 0 for n in r["wallet"].values())
                for id, r in w["desk_accounts"].items()
            ),
        )
        executed = [r for r in w["requests"].values() if r["status"] == "EXECUTED"]
        backed = {
            r.get("receipt", {}).get("id") for r in executed if isinstance(r.get("receipt"), dict)
        }
        check("operation_provenance", all(p["id"] in backed for p in w["desk_payments"].values()))
        f = c["family"]
        scope = c["acceptance"]["scope"]
        if f == "reconciliation":
            totals = defaultdict(int)
            valid = w["desk_portfolios"]["ACACIA-SEP"]["status"] == "CLOSED"
            for id, r in i["remittances"].items():
                clauses = r.get("_next_lines", r["allocation_lines"])
                rs = [
                    x
                    for x in w["invoice_allocations"].values()
                    if x["remittance_id"] == id and x["status"] == "CURRENT"
                ]
                valid &= len(rs) == 1
                if rs:
                    mapping = rs[0]["allocations"]
                    valid &= (
                        sum(mapping.values()) == r["gross_minor"] == r["net_minor"] + r["fee_minor"]
                        and set(mapping) <= set(r["eligible_invoice_ids"])
                        and all(mapping.get(iid, 0) == n for iid, n in clauses.items())
                    )
                    d = w["remittance_advice"][rs[0]["advice_id"]]
                    valid &= d["status"] == "READY" and d["revision"] == r.get(
                        "pending_revision", r["revision"]
                    )
                    for iid, n in mapping.items():
                        totals[iid] += n
            valid &= all(
                totals[x["id"]] == x["amount_minor"]
                for x in i["trade_invoices"].values()
                if x["portfolio_id"] == "ACACIA-SEP"
            )
            check("intent", valid)
        elif f == "batches":
            valid = True
            for item in i["payment_batch_items"].values():
                rs = [
                    x
                    for x in w["submission_attempts"].values()
                    if x["item_id"] == item["id"] and x.get("_bank_received")
                ]
                target = item["amount_minor"] if item["status"] == "DUE" else 0

                def receipt_states(attempt):
                    return {
                        b["status"]
                        for b in w["bank_receipts"].values()
                        if b.get("attempt_id") == attempt["id"]
                        and b.get("item_id") == item["id"]
                        and b.get("amount_minor")
                        == (
                            attempt.get("_bank_amount_minor", attempt["amount_minor"])
                            if attempt.get("_bank_received")
                            else 0
                        )
                        and b.get("bank_reference", attempt["bank_reference"])
                        == attempt["bank_reference"]
                    }

                valid &= sum(
                    x.get("_bank_amount_minor", x["amount_minor"]) for x in rs
                ) == target and all(
                    (x["status"] == "SETTLED" or "SETTLED" in receipt_states(x))
                    and x["bank_reference"] == item["bank_reference"].replace("-OLD", "-CURRENT")
                    for x in rs
                )
                valid &= not any(
                    x["status"] in ("ACK_PENDING", "SUBMITTED")
                    and not (receipt_states(x) & {"SETTLED", "NOT_RECEIVED", "REJECTED"})
                    for x in w["submission_attempts"].values()
                    if x["item_id"] == item["id"]
                )
            check("unique_payment", valid)
        elif f == "tax":
            valid = True
            totals = defaultdict(int)
            for id, t in i["tax_transfers"].items():
                rs = [
                    x
                    for x in w["tax_allocations"].values()
                    if x["transfer_id"] == id and x["status"] == "POSTED"
                ]
                valid &= (
                    len(rs) == 1
                    and rs[0]["allocations"] == t["_designated_allocations"]
                    and w["tax_transfers"][id]["amount_minor"] == t["amount_minor"]
                )
                if t["amend_until_minute"] >= 0:
                    pid = next(iter(t["_designated_allocations"]))
                    valid &= (
                        w["tax_transfers"][id]["tax_reference"]
                        == i["tax_periods"][pid]["tax_reference"]
                        and w["tax_transfers"][id]["declared_period"] == pid
                    )
                if rs:
                    for pid, n in rs[0]["allocations"].items():
                        totals[pid] += n
            check(
                "tax_posting",
                valid
                and all(totals[id] == p["amount_minor"] for id, p in i["tax_periods"].items()),
            )
            check(
                "tax_cutoffs",
                all(
                    w["minute"] >= p["filing_cutoff_minute"]
                    and cutoff_positions[pid] >= p["amount_minor"]
                    and w["tax_periods"][pid].get("cutoff_paid_minor") == cutoff_positions[pid]
                    and w["tax_periods"][pid].get("filing_status") == "DISCHARGED"
                    for pid, p in i["tax_periods"].items()
                    if "filing_cutoff_minute" in p
                ),
            )
        elif f == "onboarding":
            app = w["onboarding_applications"]["DU-APPLICATION"]
            required = set(app["direct_controllers"])
            # Propagate each person's ownership forward through a topological queue.
            edges = list(w["ownership_edges"].values())
            persons = [r for r in w["company_profiles"].values() if r["kind"] == "PERSON"]
            for person in persons:
                flow = defaultdict(Fraction)
                flow[person["id"]] = Fraction(1)
                queue = [(person["id"], Fraction(1), set())]
                while queue:
                    src, mass, seen = queue.pop()
                    if src in seen:
                        raise ValueError("ownership cycle")
                    for e in edges:
                        if e["owner_id"] == src:
                            amount = mass * Fraction(e["ownership_bp"], 10000)
                            flow[e["owned_id"]] += amount
                            queue.append((e["owned_id"], amount, seen | {src}))
                if flow[app["entity_id"]] >= Fraction(app["ownership_threshold_bp"], 10000):
                    required.add(person["id"])
            claims = {app["entity_id"]: "registry", **{x: "identity" for x in required}}
            proved = set()
            b = w["evidence_bundles"].get(app.get("bundle_id"), {})
            for sid in b.get("submission_ids", []):
                s = w["submissions"][sid]
                if (
                    s["status"] != "APPROVED"
                    or s["target_id"] != app["id"]
                    or s["contents"]["valid_until"] < app.get("reviewed_minute", 0)
                ):
                    continue
                for id, v in s["contents"]["claims"].items():
                    if v == dict(kind=claims.get(id), version=w["company_profiles"][id]["version"]):
                        proved.add(id)
            # Reserve release fees are genuine customer expenditure even when
            # the review fee is charged from the net proceeds of that release.
            # A task may offer a documented deadline-versus-fee choice. The
            # higher cap applies only after the customer approved that exact
            # fee-bearing reserve instruction, never from the agent's choice
            # or a final balance alone.
            billing_account = app["billing_account_id"]
            draw_fees = sum(
                d.get("fee_minor", 0)
                for d in w.get("reserve_disbursements", {}).values()
                if d.get("destination_account_id") == billing_account
                and d.get("scope") == c.get("scope")
            )
            cap = c["acceptance"]["max_review_fee_minor"]
            choice = c.get("customer_fee_deadline_choice", {})
            if choice:
                approved_routes = {
                    r["arguments"].get("route_id")
                    for r in w["requests"].values()
                    if r.get("operation") == "propose_reserve_disbursement"
                    and r.get("status") == "EXECUTED"
                    and r.get("approved_by")
                    and r.get("approval_seq", 10**9) < r.get("executed_seq", -1)
                }
                if choice.get("fee_route_id") in approved_routes:
                    cap = choice["accepted_total_fee_cap_minor"]
            check(
                "review_cost_and_deadline",
                sum(review_fees) + draw_fees <= cap
                and app.get("reviewed_minute", 10**9) <= c["acceptance"]["review_due_minute"],
                dict(
                    review_fees_minor=sum(review_fees),
                    reserve_draw_fees_minor=draw_fees,
                    applicable_fee_cap_minor=cap,
                    reviewed_minute=app.get("reviewed_minute"),
                ),
            )
            check(
                "evidence_set",
                app["status"] == "APPROVED"
                and set(claims) <= proved
                and app.get("reviewed_minute", 0) >= 8,
                dict(required=sorted(claims), proved=sorted(proved)),
            )
        elif f == "rails":
            valid = w["desk_accounts"]["EL-EXISTING"]["status"] == "OPEN"
            for id, s in w["rail_sites"].items():
                b = w["rail_bindings"].get(s.get("active_binding"), {})
                ac = w["desk_accounts"].get(b.get("account_id"), {})
                p = w["rail_profiles"].get(ac.get("profile_id"), {})
                valid &= (
                    s["status"] == "COLLECTING"
                    and s["sender_configuration"] == "FINAL"
                    and b.get("status") == "READY"
                    and ac.get("status") == "OPEN"
                    and ac.get("entity_id") == s["entity_id"]
                    and ac.get("holder_name") == w["company_profiles"][s["entity_id"]]["legal_name"]
                    and b.get("rail") == s["sender_rail"]
                    and s["sender_rail"] in p.get("rails", [])
                    and s["category"] in p.get("categories", [])
                    and s["market"] in p.get("markets", [])
                    and s["currency"] == p.get("currency")
                )
            check("collection_ready", valid)
            check(
                "collection_launches",
                all(
                    w["rail_bindings"]
                    .get(site.get("active_binding"), {})
                    .get("tested_minute", 10**9)
                    <= site.get("ready_due_minute", 10**9)
                    for site in w["rail_sites"].values()
                ),
            )
            check(
                "issuance_budget", issuance_fees <= c["acceptance"].get("issuance_fee_cap", 10**9)
            )
        elif f == "treasury":
            valid = True
            for oid, o in i["treasury_obligations"].items():
                ps = [p for p in w["desk_payments"].values() if p.get("obligation_id") == oid]
                valid &= sum(p["amount_minor"] for p in ps) == o["amount_minor"]
                for p in ps:
                    # Reconstruct the mandate that was effective at this payment time.
                    m = deepcopy(i["treasury_mandates"][p["mandate_id"]])
                    for ev in sorted(
                        w["events"].values(), key=lambda x: (x["due_minute"], x["id"])
                    ):
                        if (
                            ev["target_id"] != m["id"]
                            or ev["due_minute"] > p["minute"]
                            or ev["status"] != "COMPLETED"
                        ):
                            continue
                        payload = w["_event_payloads"][ev["id"]]
                        if ev["kind"] == "desk_mandate_revision":
                            m.update(payload["changes"])
                        if ev["kind"] == "desk_mandate_effective":
                            m.update(payload["evidence"]["mandate_fields"])
                            m.update(
                                version=payload["evidence"]["mandate_version"], status="EFFECTIVE"
                            )
                    valid &= (
                        p["executor_id"] == "FR-TREASURER"
                        and w["desk_accounts"][p["account_id"]]["entity_id"]
                        == o["commercial_debtor"]
                        == m["payer_entity"]
                        and p["minute"] <= o["due_minute"]
                        and p["mandate_version"] == m["version"]
                        and m["executor_id"] == p["executor_id"]
                        and m["status"] == "EFFECTIVE"
                        and m["valid_from"] <= p["minute"] <= m["valid_until"]
                        and o["beneficiary_id"] in m["beneficiaries"]
                    )
            check("institution_authority", valid)
            # Capacity and purpose independently checked per actually effective version.
            capacity_ok = True
            for p in w["desk_payments"].values():
                if p.get("kind") != "TREASURY":
                    continue
                m = deepcopy(i["treasury_mandates"][p["mandate_id"]])
                for ev in sorted(w["events"].values(), key=lambda x: (x["due_minute"], x["id"])):
                    if (
                        ev["target_id"] != m["id"]
                        or ev["due_minute"] > p["minute"]
                        or ev["status"] != "COMPLETED"
                    ):
                        continue
                    payload = w["_event_payloads"][ev["id"]]
                    if ev["kind"] == "desk_mandate_revision":
                        m.update(payload["changes"])
                    if ev["kind"] == "desk_mandate_effective":
                        m.update(payload["evidence"]["mandate_fields"])
                        m["version"] = payload["evidence"]["mandate_version"]
                used = sum(
                    x["amount_minor"]
                    for x in w["desk_payments"].values()
                    if x.get("mandate_id") == p["mandate_id"]
                    and x.get("mandate_version") == p["mandate_version"]
                    and x["minute"] <= p["minute"]
                )
                capacity_ok &= (
                    used <= m["limit_minor"]
                    and i["treasury_obligations"][p["obligation_id"]]["purpose"] in m["purposes"]
                )
            check("mandate_capacity", capacity_ok)
        elif f == "disputes":
            valid = any(d["status"] == "SETTLED" for d in w["card_disputes"].values())
            for id, x in i["loss_components"].items():
                valid &= (
                    sum(
                        e["amount_minor"]
                        for e in w["compensation_entries"].values()
                        if e["component_id"] == id and e["status"] == "SETTLED"
                    )
                    == x["loss_minor"]
                )
            check("component_loss", valid)
            check("filing_budget", filing_fees <= c["acceptance"].get("filing_fee_cap", 10**9))
            check(
                "registered_scope",
                all(
                    d["opened_minute"]
                    <= i["card_loss_cases"][d["case_id"]].get("filing_deadline_minute", 10**9)
                    and set(d["claims"]) <= set(d.get("registered_components", d["claims"]))
                    and any(
                        r["operation"] == "propose_card_dispute"
                        and r["status"] == "EXECUTED"
                        and r.get("receipt", {}).get("id") == d["id"]
                        and set(r["arguments"]["claims"]) == set(d["registered_components"])
                        for r in w["requests"].values()
                    )
                    for d in w["card_disputes"].values()
                ),
            )
            check(
                "refund_finality",
                not any(
                    e["kind"] == "desk_refund_receipt" and e["status"] == "SCHEDULED"
                    for e in w["events"].values()
                )
                and w["minute"] <= c["acceptance"].get("dispute_due_minute", 10**9),
            )
        elif f == "restrictions":
            valid = True
            spent = defaultdict(int)
            for id, o in i["purpose_instructions"].items():
                ps = [p for p in w["desk_payments"].values() if p.get("instruction_id") == id]
                valid &= sum(p["amount_minor"] for p in ps) == o["amount_minor"] and all(
                    p["minute"] <= o["due_minute"] for p in ps
                )
                for p in ps:
                    for lid, n in p["allocations"].items():
                        spent[lid] += n
                        valid &= o["purpose"] in i["restricted_lots"][lid]["purposes"]
                        for table, kind in [
                            ("fund_restrictions", "desk_restriction_release"),
                            ("receivable_rights", "desk_right_release"),
                        ]:
                            for r in i[table].values():
                                if lid not in r["lot_ids"]:
                                    continue
                                valid &= any(
                                    ev["kind"] == kind
                                    and ev["target_id"] == r["id"]
                                    and ev["status"] == "COMPLETED"
                                    and ev["due_minute"] <= p["minute"]
                                    and lid in w["_event_payloads"][ev["id"]]["lot_ids"]
                                    for ev in w["events"].values()
                                )
            valid &= all(
                w["restricted_lots"][id]["remaining_minor"] == r["amount_minor"] - spent[id] >= 0
                for id, r in i["restricted_lots"].items()
            )
            for ev in w["events"].values():
                if ev["kind"] != "desk_right_release":
                    continue
                r = i["receivable_rights"][ev["target_id"]]
                required = r.get("release_requires", {})
                if required:
                    lodged = ev["due_minute"] - r["review_delay"]
                    valid &= (
                        sum(
                            p["amount_minor"]
                            for p in w["desk_payments"].values()
                            if p.get("instruction_id") == required["instruction_id"]
                            and p["minute"] <= lodged
                        )
                        >= required["paid_minor"]
                    )
            check("lot_eligibility", valid)
        elif f == "exit":
            bills = list(w["billing_instances"].values())
            valid = (
                w["desk_accounts"]["IN-OLD"]["status"] == "CLOSED"
                and w["desk_accounts"]["IN-OLD"]["wallet"]["USD"] == 0
                and w["desk_services"]["IN-STOP"]["status"] == "TERMINATED"
                and w["desk_services"]["IN-RETAIN"]["status"] == "ACTIVE"
                and w["desk_services"]["IN-RETAIN"]["billing_account"] == "IN-KEEP"
            )
            valid &= all(
                b["status"] == ("REFUNDED" if b["id"] == "IN-EXPECTED-RETURN" else "PAID")
                for b in bills
            )
            valid &= any(
                b["service_id"] == "IN-RETAIN"
                and b["id"] not in i["billing_instances"]
                and b["status"] == "PAID"
                and b["account_id"] == "IN-KEEP"
                for b in bills
            )
            valid &= not any(
                ev["status"] == "SCHEDULED" and ev["kind"].startswith("desk_bill")
                for ev in w["events"].values()
            )
            for service, destination in c["acceptance"].get("retained_routes", {}).items():
                valid &= (
                    w["desk_services"][service]["status"] == "ACTIVE"
                    and w["desk_services"][service]["billing_account"] == destination
                    and any(
                        b["service_id"] == service
                        and b["id"] not in i["billing_instances"]
                        and b["status"] == "PAID"
                        and b["account_id"] == destination
                        for b in bills
                    )
                )
            check("exit_obligations", valid)
        elif f == "fx":
            valid = True
            for id, o in i["currency_obligations"].items():
                ps = [p for p in w["desk_payments"].values() if p.get("obligation_id") == id]
                valid &= sum(p["amount_minor"] for p in ps) == o["amount_minor"] and all(
                    p["currency"] == o["currency"]
                    and p["fee_minor"] == o["delivery_fee_minor"]
                    and p["minute"] <= o["due_minute"]
                    for p in ps
                )
            quotes = [q for q in w["fx_quotes"].values() if q["status"] == "EXECUTED"]
            for q in quotes:
                n = q["source_minor"]
                outputs = []
                for leg in q["segments"]:
                    n = int(Fraction(n * leg["numerator"], leg["denominator"]))
                    outputs.append(n)
                valid &= n == q["target_minor"] and outputs == q["leg_outputs"]
            valid &= (
                sum(q["fee_minor"] for q in quotes if q["fee_currency"] == "USD")
                <= c["acceptance"]["fx_fee_cap"]
            )
            valid &= all(
                w["desk_accounts"]["JA-WALLET"]["wallet"].get(cur, 0) >= amount
                for cur, amount in c["acceptance"]["fx_reserves"].items()
            )
            check("net_delivery", valid)
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        check("verifier_input_consistency", False, type(exc).__name__ + ": " + str(exc))
    return dict(
        success=all(x["passed"] for x in checks),
        checks=checks,
        failed=[x["name"] for x in checks if not x["passed"]],
        language_quality="separate_review",
        exhaustive_correctness_claim=False,
    )


_local_verify = verify


def denamespace(x, prefix):
    if isinstance(x, str):
        return x.removeprefix(prefix)
    if isinstance(x, list):
        return [denamespace(v, prefix) for v in x]
    if isinstance(x, dict):
        return {k.removeprefix(prefix): denamespace(v, prefix) for k, v in x.items()}
    return x


def verify(world, c):
    from payment.runtime.core.scoping import project

    scope = c["scope"]
    prefix = scope + "_"
    i = denamespace(project(c["initial_world"], scope), prefix)
    whole = denamespace(project(world, scope), prefix)
    checks = []
    for component in c["components"]:
        cc = denamespace(deepcopy(component), prefix)
        cc["initial_world"] = i
        result = _local_verify(whole, cc)
        for check in result["checks"]:
            check["component"] = cc["family"]
            check["name"] = cc["family"] + ":" + check["name"]
            checks.append(check)

    def check(name, passed, detail=None):
        checks.append(
            dict(name=name, passed=bool(passed), description_zh=LINK_LABELS[name], detail=detail)
        )

    try:
        w = whole.get("_closure_state") or whole
        a = whole["audit"]
        executed = {r["id"]: r for r in w["requests"].values() if r["status"] == "EXECUTED"}
        flows = w["reserve_disbursements"]
        valid = True
        for d in flows.values():
            route = i["reserve_routes"][d["route_id"]]
            reqs = [
                r
                for r in executed.values()
                if r["operation"] == "propose_reserve_disbursement"
                and r["arguments"] == {"route_id": d["route_id"]}
                and r.get("receipt", {}).get("id") == d["id"]
            ]
            valid &= len(reqs) == 1
            if not reqs:
                continue
            req = reqs[0]
            seq = req["executed_seq"]
            minute = d["minute"]
            valid &= d == req.get("receipt") and any(
                x["seq"] == seq
                and x["tool"] == "execute_request"
                and x["minute"] == minute
                and x["result"].get("data") == d
                for x in a
            )
            valid &= (
                all(
                    d[k] == route[k]
                    for k in (
                        "reserve_account_id",
                        "destination_account_id",
                        "debit_minor",
                        "net_minor",
                    )
                )
                and d["fee_minor"] == route["debit_minor"] - route["net_minor"]
                and d["status"] == "SETTLED"
            )
            prefix_a = [x for x in a if x["seq"] < seq and x["result"]["ok"]]
            completed_events = {
                ev["id"]
                for x in prefix_a
                if x["tool"] == "wait_for_update"
                for ev in x["result"].get("data", {}).get("events", [])
                if ev["status"] == "COMPLETED"
            }

            def event_before(kind, target):
                return any(
                    e["id"] in completed_events
                    and e["kind"] == kind
                    and e["target_id"] == target
                    and e["due_minute"] <= minute
                    for e in w["events"].values()
                )

            for condition in route["conditions"]:
                table = condition["table"]
                if condition["kind"] == "field":
                    id = condition["id"]
                    r = w[table][id]
                    value = condition["value"]
                    field = condition["field"]
                    if i[table][id].get(field) == value:
                        continue
                    valid &= r.get(field) == value
                    if table == "desk_portfolios":
                        valid &= any(
                            z["operation"] == "propose_receivable_close"
                            and z["arguments"]["portfolio_id"] == id
                            and z["executed_seq"] < seq
                            for z in executed.values()
                        )
                    elif table == "onboarding_applications":
                        valid &= r.get("reviewed_minute", 10**9) <= minute and event_before(
                            "desk_onboarding_review", id
                        )
                    elif table == "rail_sites":
                        valid &= w["rail_bindings"].get(r.get("active_binding"), {}).get(
                            "tested_minute", 10**9
                        ) <= minute and event_before("desk_rail_test", r.get("active_binding"))
                    elif table == "desk_services":
                        valid &= any(
                            e["id"] in completed_events
                            and e["due_minute"] <= minute
                            and e["kind"] == "desk_billing_move"
                            and e["target_id"] == id
                            and w["_event_payloads"][e["id"]]["account_id"] == value
                            for e in w["events"].values()
                        )
                    elif table == "tax_periods":
                        latest = {}
                        for posting in w["tax_allocations"].values():
                            if (
                                posting.get("posted_minute", 10**9)
                                > i[table][id]["filing_cutoff_minute"]
                            ):
                                continue
                            tid = posting["transfer_id"]
                            if tid not in latest or (posting["posted_minute"], posting["id"]) > (
                                latest[tid]["posted_minute"],
                                latest[tid]["id"],
                            ):
                                latest[tid] = posting
                        valid &= sum(x["allocations"].get(id, 0) for x in latest.values()) >= i[
                            table
                        ][id]["amount_minor"] and event_before("desk_tax_cutoff", id)
                    else:
                        valid = False
                else:
                    rows = [
                        r
                        for r in w[table].values()
                        if all(r.get(k) == v for k, v in condition["filters"].items())
                        and r.get("minute", r.get("received_minute", 0)) <= minute
                    ]
                    if condition.get("dedup_by"):
                        rows = list({r[condition["dedup_by"]]: r for r in rows}.values())
                    valid &= sum(r[condition["field"]] for r in rows) >= condition["minimum"]
                    for r in rows:
                        if r["id"] in i[table] and i[table][r["id"]] == r:
                            continue
                        if table == "desk_payments":
                            valid &= any(
                                z.get("receipt", {}).get("id") == r["id"]
                                and z["executed_seq"] < seq
                                for z in executed.values()
                            )
                        elif table == "bank_receipts":
                            valid &= event_before("desk_attempt_receipt", r["id"]) or event_before(
                                "desk_batch_settlement", r["attempt_id"]
                            )
                        elif table == "compensation_entries":
                            valid &= event_before("desk_dispute_settle", r["dispute_id"])
                        else:
                            valid = False
        reserve_ids = {r["reserve_account_id"] for r in i["reserve_routes"].values()}
        valid &= not any(
            r["operation"] == "propose_account_sweep"
            and r["arguments"]["source_account_id"] in reserve_ids
            for r in executed.values()
        )
        valid &= len({d["route_id"] for d in flows.values()}) == len(flows)
        check("reserve_contract_and_order", valid)
        check("reserve_terms_unchanged", w["reserve_routes"] == i["reserve_routes"])
        protected = True
        if c["case_index"] == 9:
            protected = not any(
                r["operation"] == "propose_account_sweep"
                and r["arguments"]["source_account_id"] == "CY-FEES"
                for r in executed.values()
            )
        check("customer_protected_funds", protected)
    except (KeyError, TypeError, ValueError) as exc:
        check("reserve_contract_and_order", False, str(exc))
    return dict(
        success=all(x["passed"] for x in checks),
        checks=checks,
        failed=[x["name"] for x in checks if not x["passed"]],
        language_quality="separate_review",
        exhaustive_correctness_claim=False,
    )


LINK_LABELS = dict(
    reserve_contract_and_order="Independently verify the source of each collateral payment, Net amount, Deduction, Genuine approval, And whether the pre-release milestone has occurred. ",
    reserve_terms_unchanged="Mortgage terms remain consistent with the initial public contract, Cannot succeed by changing the rules. ",
    customer_protected_funds="Comply with the customer’s explicitly protected fee account, do not complete the current action by diverting funds intended for other purposes. ",
)
LABELS.update(LINK_LABELS)
