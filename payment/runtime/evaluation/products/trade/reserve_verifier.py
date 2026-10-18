"""Independent ledger/time verification plus the audited component predicates.

No execution transition, reference planner or synthesis solver is imported.
"""

from copy import deepcopy
from collections import defaultdict
from payment.runtime.evaluation.products.payments.verifier import _local_verify, denamespace, LABELS
from payment.runtime.core.scoping import project


def verify(world, c):
    scope = c["scope"]
    prefix = scope + "_"
    i = denamespace(project(c["initial_world"], scope), prefix)
    w = denamespace(project(world, scope), prefix)
    checks = []
    for component in c["components"]:
        cc = denamespace(deepcopy(component), prefix)
        cc["initial_world"] = i
        for x in _local_verify(w, cc)["checks"]:
            x["component"] = cc["family"]
            x["name"] = cc["family"] + ":" + x["name"]
            checks.append(x)

    def check(name, value, detail=None):
        dimensions = {
            "reserve.booking_provenance": "integrity",
            "reserve.terms_at_execution": "process",
            "reserve.settlement_and_fees": "outcome",
        }
        row = dict(name=name, passed=bool(value), description_zh=LABELS[name], detail=detail)
        if name in dimensions:
            row["dimension"] = dimensions[name]
        checks.append(row)

    try:
        s = w.get("_closure_state") or w
        routes = i["reserve_routes"]
        audit = w["audit"]
        bookings = s["reserve_disbursements"]
        provenance_ok = True
        terms_ok = True
        settlement_ok = True
        debits = defaultdict(int)
        credits = defaultdict(int)
        used = set()
        for d in bookings.values():
            r = deepcopy(routes[d["route_id"]])
            receipts = [
                a
                for a in audit
                if a["tool"] == "execute_request"
                and a["result"].get("data", {}).get("id") == d["id"]
                and a["result"].get("ok")
            ]
            requests = [
                q
                for q in s["requests"].values()
                if q.get("status") == "EXECUTED"
                and q.get("operation") == "propose_reserve_disbursement"
                and q.get("receipt", {}).get("id") == d["id"]
            ]
            provenance_ok &= bool(receipts) and len(requests) == 1 and d["route_id"] not in used
            used.add(d["route_id"])
            if not receipts or len(requests) != 1:
                continue
            request = requests[0]
            seq = receipts[0]["seq"]
            # Successful retries return the original immutable receipt. They
            # are observations of one posting, not additional economic writes.
            provenance_ok &= (
                request["arguments"] == dict(route_id=d["route_id"])
                and request["executed_seq"] == seq
            )
            provenance_ok &= all(
                a["actor"] == "assistant"
                and a["arguments"] == dict(request_id=request["id"])
                and a["result"]["data"] == request["receipt"]
                for a in receipts
            )
            booking = next(a for a in audit if a["seq"] == seq)
            clock = max(
                [i["minute"]]
                + [
                    a["result"]["data"]["minute"]
                    for a in audit
                    if a["seq"] < seq and a["tool"] == "wait_for_update" and a["result"]["ok"]
                ]
            )
            provenance_ok &= d["minute"] == booking["minute"] == clock
            provenance_ok &= all(
                booking["result"]["data"].get(k) == d.get(k)
                for k in (
                    "route_id",
                    "reserve_account_id",
                    "destination_account_id",
                    "debit_minor",
                    "net_minor",
                    "minute",
                    "settles_minute",
                    "terms_version",
                )
            )
            seen = [
                ev
                for a in audit
                if a["seq"] < seq and a["tool"] == "wait_for_update" and a["result"]["ok"]
                for ev in a["result"]["data"].get("events", [])
            ]
            if any(
                e["kind"] == "desk_reserve_assessment"
                and e["target_id"] == r.get("assessment_group")
                for e in seen
            ):
                r.update(r["_assessed_terms"])
                r["terms_version"] = r.get("terms_version", 1) + 1
            settlement_ok &= all(
                d[k] == r[k]
                for k in (
                    "reserve_account_id",
                    "destination_account_id",
                    "debit_minor",
                    "net_minor",
                )
            )
            terms_ok &= d["terms_version"] == r.get("terms_version", 1)
            settlement_ok &= d["fee_minor"] == r["debit_minor"] - r["net_minor"] >= 0
            terms_ok &= (
                r.get("available_from", 0) <= d["minute"] <= r.get("expires_minute", 1000000)
            )
            settlement_ok &= d["settles_minute"] == d["minute"] + r.get("settlement_delay", 0)
            # Reconstruct milestone truth at the booking prefix using receipts/events.
            state = deepcopy(i)
            for a in audit:
                if a["seq"] >= seq:
                    break
                if not a["result"]["ok"]:
                    continue
                if a["tool"] == "wait_for_update":
                    for ev in a["result"]["data"].get("events", []):
                        if ev["kind"] == "desk_onboarding_review":
                            rr = s["onboarding_applications"][ev["target_id"]]
                            if rr.get("reviewed_minute", 10**9) <= ev["due_minute"]:
                                state["onboarding_applications"][ev["target_id"]] = rr
                        elif ev["kind"] == "desk_rail_test":
                            b = s["rail_bindings"][ev["target_id"]]
                            state["rail_sites"][b["site_id"]] = dict(
                                state["rail_sites"][b["site_id"]],
                                status="COLLECTING" if b["status"] == "READY" else "NOT_READY",
                            )
                data = a["result"].get("data")
                if a["tool"] == "execute_request" and isinstance(data, dict) and "id" in data:
                    for table in ("desk_portfolios", "desk_payments"):
                        if data["id"] in s[table]:
                            state[table][data["id"]] = deepcopy(data)
            for cond in r["conditions"]:
                rows = state[cond["table"]]
                if cond["kind"] == "field":
                    terms_ok &= rows[cond["id"]].get(cond["field"]) == cond["value"]
                else:
                    vals = [
                        x
                        for x in rows.values()
                        if all(x.get(k) == v for k, v in cond["filters"].items())
                    ]
                    if cond.get("dedup_by"):
                        vals = list({x[cond["dedup_by"]]: x for x in vals}.values())
                    terms_ok &= sum(x.get(cond["field"], 0) for x in vals) >= cond["minimum"]
            settlement_ok &= d["status"] == "SETTLED" and d["settles_minute"] <= s["minute"]
            if r.get("settlement_delay", 0):
                settlement_ok &= any(
                    ev["kind"] == "desk_reserve_settlement"
                    and ev["target_id"] == d["id"]
                    and ev["due_minute"] == d["settles_minute"]
                    and ev["status"] == "COMPLETED"
                    for ev in s["events"].values()
                )
            debit_rows = [
                j
                for j in s["desk_journal"].values()
                if j["reference"] == d["route_id"]
                and j["reason"] == "reserve_disbursement"
                and j["delta_minor"] < 0
            ]
            credit_rows = [
                j
                for j in s["desk_journal"].values()
                if j["reference"] == d["route_id"]
                and j["reason"] == "reserve_disbursement"
                and j["delta_minor"] > 0
            ]
            provenance_ok &= len(debit_rows) == len(credit_rows) == 1
            if debit_rows and credit_rows:
                provenance_ok &= (
                    debit_rows[0]["account_id"] == d["reserve_account_id"]
                    and credit_rows[0]["account_id"] == d["destination_account_id"]
                    and debit_rows[0]["minute"] == d["minute"]
                    and debit_rows[0]["delta_minor"] == -d["debit_minor"]
                    and credit_rows[0]["delta_minor"] == d["net_minor"]
                    and credit_rows[0]["minute"] == d["settles_minute"]
                )
            debits[d["reserve_account_id"]] += d["debit_minor"]
            credits[d["destination_account_id"]] += d["net_minor"]
        for rid in {r["reserve_account_id"] for r in routes.values()}:
            provenance_ok &= (
                s["desk_accounts"][rid]["wallet"]["USD"]
                == i["desk_accounts"][rid]["wallet"]["USD"] - debits[rid]
                >= 0
            )
        if c.get("_dimension_registry") == "bizagentbench-v1":
            check("reserve.booking_provenance", provenance_ok)
            check("reserve.terms_at_execution", terms_ok)
            check("reserve.settlement_and_fees", settlement_ok)
        else:
            check("reserve_ledger_and_booking", provenance_ok and terms_ok and settlement_ok)
        expected = deepcopy(routes)
        for r in expected.values():
            evs = [
                e
                for e in s["events"].values()
                if e["kind"] == "desk_reserve_assessment"
                and e["target_id"] == r.get("assessment_group")
                and e["status"] == "COMPLETED"
            ]
            if evs:
                r.update(r["_assessed_terms"])
                r.update(
                    pending_assessment=False,
                    terms_version=r.get("terms_version", 1) + 1,
                    assessed_minute=evs[0]["due_minute"],
                )
                r["fee_minor"] = r["debit_minor"] - r["net_minor"]
        check("published_terms_history", s["reserve_routes"] == expected)
        allowed = set(c.get("authorized_sweep_sources", []))
        allowed = {x.removeprefix(prefix) for x in allowed}
        # Explicit customer restriction; legal bank sweeps may violate the case.
        sweep = [
            r
            for r in s["requests"].values()
            if r["status"] == "EXECUTED" and r["operation"] == "propose_account_sweep"
        ]
        check("funding_scope", all(r["arguments"]["source_account_id"] in allowed for r in sweep))
    except (KeyError, TypeError, ValueError) as exc:
        check(
            (
                "reserve.booking_provenance"
                if c.get("_dimension_registry") == "bizagentbench-v1"
                else "reserve_ledger_and_booking"
            ),
            False,
            str(exc),
        )
    if c.get("minimize_reserve_fees"):
        from payment.runtime.products.constraints.cost_checks import lower_bound

        try:
            bound = lower_bound(
                i,
                list(i["reserve_routes"].values()),
                denamespace(c["funding_requirements"], prefix),
            )
            paid = sum(
                d["debit_minor"] - d["net_minor"]
                for d in (w.get("_closure_state") or w)["reserve_disbursements"].values()
            )
            checks.append(
                dict(
                    name="reserve_total_fee_optimal",
                    passed=paid == bound["minimum_fee_minor"],
                    description_zh="After completing all business, Total actual two withdrawal fees reach the independent fee lower bound of the public contract space; Reference full replay separately proves the lower bound is reachable. ",
                    detail=dict(actual_fee_minor=paid, **bound),
                )
            )
        except (KeyError, ValueError, AssertionError) as exc:
            checks.append(
                dict(
                    name="reserve_total_fee_optimal",
                    passed=False,
                    description_zh="Unable to verify joint fee optimality. ",
                    detail=str(exc),
                )
            )
    return dict(
        success=all(x["passed"] for x in checks),
        checks=checks,
        failed=[x["name"] for x in checks if not x["passed"]],
        exhaustive_correctness_claim=False,
        language_quality="separate_review",
    )


LABELS.update(
    **{
        "reserve_ledger_and_booking": "Independently reconcile collateral principal deduction, Net amount posting, Non-refundable fee, Arrival time, Version and business facts before release. ",
        "reserve.booking_provenance": "Reconcile each real request one by one, Execution receipt., Ledger entries and source balance. ",
        "reserve.terms_at_execution": "At the execution of each disbursement, Route valid, Terms version is correct and release conditions are actually satisfied. ",
        "reserve.settlement_and_fees": "Appropriation actually credited, Payee, Principal and non-refundability comply with the then-current contract. ",
        "published_terms_history": "Term changes originate only from pre-fixed bank assessment events; Committed withdrawals are bound to the then-current version. ",
        "funding_scope": "Use only customer-authorized collateral sources and business accounts; Even if account transfer is legal at bank, cannot arbitrarily expand source of funds. ",
    }
)
