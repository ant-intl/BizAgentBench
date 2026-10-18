from copy import deepcopy
from payment.runtime.evaluation.psp.commerce.linked_obligations import (
    wrap as _with_linked_obligations,
)
from payment.runtime.evaluation.psp.commerce.business_constraints import (
    wrap as _with_business_constraints,
)
from payment.runtime.evaluation.psp.commerce.dependencies import wrap as _with_dependencies
from payment.runtime.evaluation.psp.commerce.merchant_settlement import (
    wrap as _with_reasoning_commerce,
)
from payment.runtime.evaluation.psp.commerce.financing import wrap as _with_reasoning_finance
from payment.runtime.evaluation.psp.commerce.ledger import wrap as _with_reasoning_ledgers
from payment.runtime.evaluation.psp.commerce.routing import wrap as _with_reasoning_routes


def verify(world, contract):
    """Recompute terminal and historical predicates from original economic data.

    No transition or reference strategy is imported or called here. The original
    contract supplies immutable evidence; actual journals supply execution history.
    """
    n = contract["number"]
    a = contract["acceptance"]
    initial = contract["initial_world"]
    checks = []

    def rs(t):
        return list(world.get("xp_c_" + t, {}).values())

    def old(t, ident):
        return initial["xp_c_" + t][ident]

    def one(t, ident):
        return world["xp_c_" + t][ident]

    journals = list(world.get("xp_journal", {}).values())
    cashrows = [j for j in journals if j.get("record_type") == "cash_posting"]

    def fees(*kinds):
        return -sum((j["amount_minor"] for j in cashrows if j["kind"] in kinds))

    def check(label, passed, detail, zh):
        checks.append(
            dict(
                name=f"c{n}_{label}",
                passed=bool(passed),
                detail=detail,
                explanation={"check_zh": zh, "rule_documents": [f"PX-C-{n}"]},
            )
        )

    if n == 43:
        card = one("network_cards", a["card"])
        essential = [one("network_tokens", i) for i in a["essential"]]
        stop = one("network_tokens", a["stop"])
        ready = card["active"] and all(
            (
                t["service_active"]
                and t["agreement_active"]
                and (t["status"] == "ACTIVE")
                and (t["generation"] == card["generation"])
                for t in essential
            )
        )
        check(
            "goal",
            ready,
            [
                {
                    "billed": t.get("billed"),
                    "agreement_active": t["agreement_active"],
                    "status": t["status"],
                }
                for t in essential
            ],
            "Necessary services, Valid contract and current card credentials remain available; The customer is not forced to run the charging cycle immediately",
        )
        check(
            "lifecycle",
            card["active"]
            and card["generation"] > old("network_cards", a["card"])["generation"]
            and all(
                (
                    t["generation"] == card["generation"] and t["status"] == "ACTIVE"
                    for t in essential
                )
            ),
            card["generation"],
            "Card replacement completed and necessary token corresponds to new card generation",
        )
        check(
            "scope",
            not stop["agreement_active"] and (not stop["billed"]),
            {"agreement": stop["agreement_active"], "charged": stop["billed"]},
            "unneeded merchant contracts have been terminated with no continued deductions",
        )
        expected_charges = sorted(
            (old("network_tokens", t["id"])["amount_minor"] for t in essential if t.get("billed"))
        )
        actual_charges = sorted(
            (-j["amount_minor"] for j in cashrows if j["kind"] == "merchant_recurring_charge")
        )
        check(
            "amount",
            actual_charges == expected_charges,
            {"actual": actual_charges, "expected": expected_charges},
            "If charges actually occur, Item-by-item amounts and counts match the true charged status of necessary merchants; Allow temporary non-charging",
        )
    elif n == 44:
        b = one("cod_batches", a["target"])
        sources = list(initial["xp_c_cod_orders"].values())
        expected = sum(
            (
                o["collected_from_buyer_minor"]
                - (o["commission_minor"] if o["delivery_status"] == "DELIVERED" else 0)
                for o in sources
            )
        )
        expected_cash = sum((o["_counted_cash_minor"] - o["commission_minor"] for o in sources))
        short = sum((o["collected_from_buyer_minor"] - o["_counted_cash_minor"] for o in sources))
        check(
            "goal",
            b.get("cash_remitted")
            and (
                b.get("shortage_disposition") == "carrier_shortfall"
                if short
                else not b.get("shortage_disposition")
            ),
            b.get("shortage_disposition"),
            "Cash and carrier shortfalls are collected separately",
        )
        check(
            "amount",
            b["remitted_minor"] == expected_cash
            and b["recovered_minor"] == short
            and (b["remitted_minor"] + b["recovered_minor"] == expected),
            {"net": b["remitted_minor"] + b["recovered_minor"], "expected": expected},
            "Calculate complete net revenue from order commissions and actual buyer payments",
        )
        check(
            "custody",
            b.get("inventoried")
            and all(
                (
                    o.get("actual_cash_minor") == old("cod_orders", o["id"])["_counted_cash_minor"]
                    for o in rs("cod_orders")
                )
            )
            and (not b.get("waived", False)),
            b.get("inventoried"),
            "True inventory receipt and correct shortfall liability are preserved",
        )
        check(
            "cost",
            fees("carrier_inventory_fee") <= a["fee_cap_minor"],
            fees("carrier_inventory_fee"),
            "Stocktaking cost is within budget",
        )
        check(
            "journal",
            -fees("carrier_cash_remittance", "carrier_shortage_recovery") == expected,
            -fees("carrier_cash_remittance", "carrier_shortage_recovery"),
            "Actual credit equals full contract net amount.",
        )
    elif n == 45:
        b = one("tax_batches", a["target"])
        sources = list(initial["xp_c_tax_orders"].values())
        expected = {
            o["id"]: (
                o["tax_minor"] - o["_final_refunded_tax_minor"]
                if o["_final_collector"] == "MERCHANT"
                else 0
            )
            for o in sources
        }
        actual = {
            o["id"]: sum(
                (r["amount_minor"] for r in rs("tax_remittances") if r["order_id"] == o["id"])
            )
            for o in sources
        }
        check(
            "goal",
            b["status"] == "CLOSED" and b["events_current"],
            b["status"],
            "Complete account closing after importing actual order corrections.",
        )
        check(
            "liability",
            actual == expected,
            {"actual": actual, "expected": expected},
            "Independently recalculate each order based on final collection-on-behalf liability and returns",
        )
        check(
            "revenue",
            b.get("published_net_minor") == sum((o["goods_minor"] for o in sources)),
            b.get("published_net_minor"),
            "Net income retains all goods revenue and has no duplicate tax.",
        )
        credit = sum((x.get("credit_minor", 0) for x in rs("tax_remittances")))
        check(
            "journal",
            fees("sales_tax_remittance") + credit == sum(expected.values()),
            {"cash": fees("sales_tax_remittance"), "prepaid_credit": credit},
            "Sum of actual bank remittance and utilized prepayment credit equals merchant tax liability",
        )
    elif n == 46:
        ids = a["statements"]
        statements = [one("statements", i) for i in ids]
        entries = [e for e in rs("statement_entries") if e["statement_id"] in ids]
        check(
            "goal",
            all(
                (
                    s["status"] == "CLOSED" and s.get("report_method") == "complete"
                    for s in statements
                )
            ),
            [s.get("report_method") for s in statements],
            "Both account transaction sets complete integrity certification rather than only checking identical opening and closing balances",
        )
        complete = True
        arith = True
        for s in statements:
            original = old("statements", s["id"])
            source = original["_original_entries"]
            current = [e for e in entries if e["statement_id"] == s["id"]]
            byseq = {e["sequence"]: e for e in current}
            complete &= (
                len(current) == len(source)
                and set(byseq)
                == set(range(original["first_sequence"], original["last_sequence"] + 1))
                and all(
                    (
                        e["sequence"] in byseq
                        and all(
                            (
                                byseq[e["sequence"]].get(k) == e[k]
                                for k in ("bank_reference", "amount_minor", "narrative")
                            )
                        )
                        for e in source
                    )
                )
            )
            arith &= (
                original["opening_minor"] + sum((e["amount_minor"] for e in current))
                == original["closing_minor"]
            )
        check(
            "completeness",
            complete,
            {
                s["id"]: sorted((e["sequence"] for e in entries if e["statement_id"] == s["id"]))
                for s in statements
            },
            "Independently verify the original sequence and original amount of both accounts, No mixed sequence",
        )
        expected_count = sum((len(old("statements", i)["_original_entries"]) for i in ids))
        check(
            "attribution",
            len(entries) == expected_count
            and all(
                (
                    e.get("category") == a["category_by_reference"].get(e["bank_reference"])
                    for e in entries
                )
            ),
            {e["bank_reference"]: e.get("category") for e in entries},
            "Supplier refund, Payment for goods and another account’s handling fee are correctly attributed separately",
        )
        check(
            "archive",
            all((s["archive_status"] == "AVAILABLE" for s in statements)),
            [s["archive_status"] for s in statements],
            "retain the original bank records for both accounts",
        )
        check(
            "cost",
            fees("statement_retrieval") <= a["fee_cap_minor"],
            fees("statement_retrieval"),
            "Select the supplementary collection scope based on actual shortfall, Shared budget for two accounts",
        )
        check(
            "arithmetic",
            arith,
            {
                s["id"]: sum((e["amount_minor"] for e in entries if e["statement_id"] == s["id"]))
                for s in statements
            },
            "For each account, recalculate opening and closing balances from the complete original transaction records",
        )
    elif n == 47:
        dispositions = {a["target"]: "SETTLED", a["return_execution"]: "RETURNED"}
        goal = True
        valid = True
        economic = True
        receipts = True
        detail = []
        for ident in a["executions"]:
            ex = one("executions", ident)
            src = old("executions", ident)
            order = one("recovery_orders", src["order_id"])
            postings = [p for p in rs("postings") if p["execution_id"] == ident]
            movements = [m for m in rs("bank_movements") if m["execution_id"] == ident]
            returned = dispositions[ident] == "RETURNED"
            expected_components = {"payable_debit", "cash_credit"} | (
                {"reversal_payable_credit", "reversal_cash_debit"} if returned else set()
            )
            expected_return = next(
                (
                    ev["_payload"]["return_reference"]
                    for ev in initial.get("xp_events", {}).values()
                    if ev.get("kind") == "c_accounting_bank_return" and ev["subject_id"] == ident
                ),
                None,
            )
            goal &= (
                order["status"] == ("RETURNED" if returned else "PAID")
                and ex["bank_status"] == dispositions[ident]
            )
            valid &= (
                len(postings) == len(expected_components)
                and {p["component"] for p in postings} == expected_components
                and all(
                    (
                        p["amount_minor"] == src["amount_minor"]
                        and p["source_receipt"]
                        == (
                            expected_return
                            if p["component"].startswith("reversal_")
                            else src["receipt_number"]
                        )
                        for p in postings
                    )
                )
            )
            economic &= (
                len(movements) == (2 if returned else 1)
                and sum(
                    (
                        m["direction"] == "DEBIT" and m["amount_minor"] == src["amount_minor"]
                        for m in movements
                    )
                )
                == 1
                and (
                    sum(
                        (
                            m["direction"] == "CREDIT"
                            and m["amount_minor"] == src["amount_minor"]
                            and (m.get("reason") == "bank_return")
                            for m in movements
                        )
                    )
                    == int(returned)
                )
                and (not ex["compensated"])
            )
            receipts &= ex["receipt_available"]
            detail.append(
                {
                    "execution": ident,
                    "bank": ex["bank_status"],
                    "order": order["status"],
                    "components": [p["component"] for p in postings],
                    "movements": len(movements),
                }
            )
        check(
            "goal",
            goal,
            detail,
            "The original valid payment remains paid, Another payment actually returned by the bank is projected as returned.",
        )
        check(
            "postings",
            valid,
            detail,
            "Recompute the original debit and credit entries for each execution voucher and the reversing entries required only for actual returns.",
        )
        check(
            "economic_once",
            economic,
            detail,
            "Each original payment is executed only once; Only a bank fixed return event generates one corresponding refund",
        )
        returned_amount = old("executions", a["return_execution"])["amount_minor"]
        expected_cash = initial["xp_accounts"][a["account_id"]]["balance_minor"] + returned_amount
        initial_journal = initial.get("xp_journal", {})
        new_cash = [j for j in cashrows if j["id"] not in initial_journal]
        return_event = next(
            (
                ev
                for ev in initial["xp_events"].values()
                if ev.get("kind") == "c_accounting_bank_return"
                and ev["subject_id"] == a["return_execution"]
            )
        )
        source_account = initial["xp_accounts"][a["account_id"]]
        valid_return = (
            len(new_cash) == 1
            and all(
                (
                    j["kind"] == "payment_bank_return"
                    and j.get("event_id") == return_event["id"]
                    and (not j.get("request_id"))
                    and (j["account_id"] == a["account_id"])
                    and (j["currency"] == source_account["currency"])
                    and (j["amount_minor"] == returned_amount)
                    and (j["reference_id"] == a["return_execution"])
                    for j in new_cash
                )
            )
            and (world["xp_events"][return_event["id"]]["status"] == "COMPLETED")
        )
        check(
            "cash_unchanged",
            world["xp_accounts"][a["account_id"]]["balance_minor"] == expected_cash
            and valid_return,
            {
                "actual": world["xp_accounts"][a["account_id"]]["balance_minor"],
                "expected": expected_cash,
                "new_cash_postings": [
                    {
                        k: j.get(k)
                        for k in (
                            "id",
                            "account_id",
                            "kind",
                            "amount_minor",
                            "event_id",
                            "request_id",
                            "reference_id",
                        )
                    }
                    for j in new_cash
                ],
                "preserved_initial_cash_rows": sum((j["id"] in initial_journal for j in cashrows)),
            },
            "Manual recovery does not move cash, The only newly added cash must come exactly from the account of the originally specified return event, amount and execution evidence; Existing other customer transactions are not counted as new cash this time",
        )
        check(
            "receipt",
            receipts,
            receipts,
            "Retain the vouchers required for the two original executions and refund recovery",
        )
    elif n == 48:
        b = one("negotiations", a["target"])
        critical = one("negotiation_obligations", a["critical"])
        flexible = one("negotiation_obligations", a["flexible"])
        check(
            "goal",
            critical["status"] == flexible["status"] == "PAID",
            {"critical": critical["status"], "flexible": flexible["status"]},
            "After negotiation of the infeasible original plan, both items are still actually completed",
        )
        check(
            "principal",
            all(
                (
                    o.get("paid_minor")
                    == old("negotiation_obligations", o["id"])["original_amount_minor"]
                    for o in (critical, flexible)
                )
            ),
            [o.get("paid_minor") for o in (critical, flexible)],
            "Negotiation does not diminish the full principal the customer insists on",
        )
        check(
            "deadlines",
            critical.get("paid_minute", 10**9) <= a["critical_deadline"]
            and flexible.get("paid_minute", 10**9)
            <= min(flexible["due_minute"], a["flexible_deadline"]),
            [o.get("paid_minute") for o in (critical, flexible)],
            "Fixed priority is fulfilled according to original term, Pay flexible items within the consented deadline",
        )
        adopted = world["xp_c_negotiation_plans"].get(b.get("adopted_plan_id"), {})
        check(
            "consent_terms",
            adopted.get("obligation_id") == flexible["id"]
            and adopted.get("revised_amount_minor")
            == old("negotiation_obligations", flexible["id"])["original_amount_minor"]
            and (adopted.get("revised_due_minute", 10**9) <= a["flexible_deadline"]),
            adopted.get("name"),
            "The specific changes of the approved binding belong to the set previously allowed by the user",
        )
        check(
            "cost",
            fees("plan_negotiation_fee") <= a["fee_cap_minor"],
            fees("plan_negotiation_fee"),
            "Rescheduled plan fees comply with the customer budget",
        )
    elif n == 49:
        o = one("route_offers", a["target"])
        src = old("route_offers", a["target"])
        inquiries = rs("route_inquiries")
        total = sum((i["fee_minor"] for i in inquiries)) + o.get("route_fee_minor", 0)
        check(
            "goal",
            o["status"] == "PAID" and o.get("paid_minor") == src["principal_minor"],
            o["status"],
            "Principal paid in full",
        )
        check(
            "informed",
            o.get("committed_route") in ("conservative", "premium")
            or (
                o.get("committed_route") == "economical"
                and any((i["result"] == "ELIGIBLE" for i in inquiries))
            ),
            o.get("committed_route"),
            "Commitments are supported by true information available at the time, Rather than after-the-fact guessing",
        )
        check(
            "cost",
            total <= src["fee_cap_minor"],
            total,
            "Verify that the sum of cost and payment fees does not exceed the publicly approved budget",
        )
        check(
            "deadline",
            o.get("paid_minute", 10**9) <= src["deadline_minute"],
            o.get("paid_minute"),
            "Payment is completed before the public deadline.",
        )
    else:
        c = one("controllers", a["target"])
        actions = rs("automation_actions")
        wallet = world["xp_accounts"][c["wallet_account_id"]]["balance_minor"]
        top = c["topup"]
        sweep = c["sweep"]
        check(
            "goal",
            c["purchase_paid"]
            and len([j for j in cashrows if j["kind"] == "wallet_business_purchase"]) == 1,
            c["purchase_paid"],
            "The business payment is actually executed exactly once",
        )
        check(
            "services",
            top["enabled"] and sweep["enabled"],
            {"topup": top["enabled"], "sweep": sweep["enabled"]},
            "Retain the two automatic services required for normal operations",
        )
        check(
            "stable",
            top["threshold_minor"] <= wallet <= sweep["threshold_minor"],
            wallet,
            "Final balance lies in the stable interval between the two actual triggers",
        )
        configuration = {
            name: deepcopy(old("controllers", c["id"])[name]) for name in ("topup", "sweep")
        }
        last_configured_cycle = 0
        requests = sorted(
            world.get("xp_requests", {}).values(), key=lambda r: r.get("executed_seq", 0)
        )
        for request in requests:
            if (
                request["status"] != "EXECUTED"
                or request["operation"] != "configure_wallet_automation"
                or request["arguments"]["controller_id"] != c["id"]
            ):
                continue
            args = request["arguments"]
            name = args["rule"]
            next_rule = {
                key: args[key]
                for key in ("threshold_minor", "amount_minor", "enabled", "cooldown_cycles")
            }
            if next_rule != configuration[name]:
                last_configured_cycle = request["receipt"]["cycles_run"]
            configuration[name] = next_rule
        observed_cycles = c["cycles_run"] - last_configured_cycle
        check(
            "observation",
            observed_cycles >= a["observation_cycles"],
            {
                "cycles_after_configuration": observed_cycles,
                "last_configured_cycle": last_configured_cycle,
            },
            "After final actual configuration, prescribed observation period has been fully executed",
        )
        cost = sum((x["fee_minor"] for x in actions))
        check(
            "cost",
            cost <= a["fee_cap_minor"],
            cost,
            "Actual automatic action fees are within the customer cap",
        )
        observed_actions = [x for x in actions if x["cycle"] > last_configured_cycle]
        pointless_cycles = []
        for cycle in {x["cycle"] for x in observed_actions}:
            current = [x for x in observed_actions if x["cycle"] == cycle]
            retained = sum(
                (
                    (
                        x["amount_minor"] - x["fee_minor"]
                        if x["rule"] == "topup"
                        else -x["amount_minor"]
                    )
                    for x in current
                )
            )
            if {x["rule"] for x in current} == {"topup", "sweep"} and retained <= 0:
                pointless_cycles.append(cycle)
        check(
            "feedback",
            not pointless_cycles,
            {
                "pointless_cycles": sorted(pointless_cycles),
                "observed_actions": len(observed_actions),
            },
            "Within the final configuration observation window, there is no invalid loop with non-positive post-payment net retention.",
        )
    return dict(success=all((c["passed"] for c in checks)), checks=checks)


from payment.runtime.evaluation.pipeline import VerificationPipeline

verify = VerificationPipeline(
    verify,
    (
        _with_linked_obligations,
        _with_business_constraints,
        _with_dependencies,
        _with_reasoning_commerce,
        _with_reasoning_finance,
        _with_reasoning_ledgers,
        _with_reasoning_routes,
    ),
)
