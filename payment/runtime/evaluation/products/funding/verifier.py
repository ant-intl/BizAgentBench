"""Independent contractual and conservation checks. No engine or reference-policy imports."""

from copy import deepcopy
from payment.runtime.evaluation.products.review.verifier import verify as component_verify
from payment.runtime.products.funding.records import FACILITY_TABLES


def verify(world, c):
    if not c.get("joint_programme"):
        return component_verify(world, c)
    state = world.get("_closure_state") or world
    initial = c["initial_world"]
    checks = []

    def check(name, ok, zh, detail=None):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, detail=detail))

    try:
        order = initial["programme_orders"][c["order_id"]]
        transfers = [
            t for t in state["programme_transfers"].values() if t["order_id"] == order["id"]
        ]
        routes = initial["programme_routes"]
        accounts = initial["programme_accounts"]
        fees = 0
        credits = order["fee_credits_remaining"]
        credit_ok = True
        balances = {k: r["balance_minor"] for k, r in accounts.items() if r["scope"] == c["scope"]}
        eligible = True
        receipts = True
        version_ok = True
        settled = True
        approval_requests = {
            q.get("receipt", {}).get("id"): q
            for q in state["requests"].values()
            if q.get("operation") == "propose_programme_draw" and q.get("status") == "EXECUTED"
        }
        main_books = [
            b
            for b in state["service_bookings"].values()
            if b["case_id"] == order["trigger_case_id"]
        ]
        first_seqs = [
            a["seq"]
            for a in world["audit"]
            if a["tool"] == "execute_request"
            and a["result"].get("ok")
            and a["result"].get("data", {}).get("id") in {b["id"] for b in main_books}
        ]
        for t in sorted(
            transfers, key=lambda t: approval_requests.get(t["id"], {}).get("executed_seq", 10**12)
        ):
            route = routes[t["route_id"]]
            source = accounts[route["account_id"]]
            version = t["terms_version"]
            terms = route if version == 1 else route.get("_assessed", {})
            q = approval_requests.get(t["id"])
            receipts &= bool(q)
            if q:
                args = q["arguments"]
                receipts &= all(
                    args.get(k) == t.get(k)
                    for k in (
                        "order_id",
                        "route_id",
                        "destination_case_id",
                        "amount_minor",
                        "terms_version",
                    )
                )
                version_ok &= version == 1 or (
                    version == 2 and bool(first_seqs) and min(first_seqs) < q["executed_seq"]
                )
            else:
                version_ok = False
            required = terms.get("fee_credits_required", 0)
            consumed = required if credits >= required else 0
            fee = (
                terms.get("fee_minor", -1)
                if consumed == required
                else terms.get("fallback_fee_minor", -1)
            )
            credits -= consumed
            credit_ok &= t.get("fee_credits_consumed") == consumed
            delay = terms.get("settlement_delay", -1)
            fees += fee
            balances[source["id"]] -= t["amount_minor"] + fee
            eligible &= t["destination_case_id"] in source["permitted_case_ids"] and t[
                "destination_case_id"
            ] in route.get("permitted_destination_ids", order["capital_by_case"])
            settled &= (
                t["status"] == "SETTLED"
                and t.get("settled_minute") == t["submitted_minute"] + delay
                and t.get("settled_minute", 10**9) <= order["settlement_deadline"]
            )
            receipts &= (
                t["fee_minor"] == fee and t["account_id"] == source["id"] and t["amount_minor"] > 0
            )
        check(
            "programme.capital",
            all(
                0
                <= sum(t["amount_minor"] for t in transfers if t["destination_case_id"] == cid)
                <= n
                for cid, n in order["capital_by_case"].items()
            ),
            "Fund allocation is the principal cap; Withdrawing less funds according to actual needs can pass, The two original transactions must still each be completed. ",
        )
        check(
            "programme.fee_credits",
            credit_ok
            and credits >= 0
            and state["programme_orders"][order["id"]]["fee_credits_remaining"] == credits,
            "Independently reconcile shared discount consumption and cash fees in case of shortfall according to actual execution order. ",
            dict(remaining=credits),
        )
        check(
            "programme.settlement_window",
            settled,
            "Both cash deliveries completed within acceptance window of public funding instruction. ",
        )
        check(
            "programme.ownership",
            eligible,
            "Each withdrawal meets the agreed source, purpose, route, and destination., system acceptance cannot replace commercial attribution. ",
        )
        check(
            "programme.version",
            version_ok,
            "the updated quote takes effect only after a real business trigger event; Future conditions cannot be used in advance. ",
        )
        check(
            "programme.receipts",
            receipts
            and len(transfers)
            == len(
                [q for q in approval_requests.values() if q["arguments"]["order_id"] == order["id"]]
            ),
            "Each fund entry corresponds to an actual execution request, independently verify the amount, Quote version, Fees and sources.. ",
        )
        check(
            "programme.conservation",
            all(
                n >= 0 and n == state["programme_accounts"][aid]["balance_minor"]
                for aid, n in balances.items()
            ),
            "From the initial arrangement balance and per-transaction principal, Contract fees independently rebuild balance. ",
            dict(expected_balances=balances),
        )
        check(
            "programme.budget",
            fees <= order["fee_cap_minor"]
            and sum(balances.values()) >= order["minimum_reserve_minor"],
            "The total fees of the cash arrangement and the remaining reserve fund simultaneously satisfy the customer agreement. ",
            dict(fees_minor=fees, reserve_minor=sum(balances.values())),
        )
        immutable = True
        for table in FACILITY_TABLES:
            for rid, row in initial[table].items():
                current = state[table].get(rid, {})
                allowed = (
                    (
                        {"balance_minor"}
                        if table == "programme_accounts"
                        else (
                            {"fee_minor", "settlement_delay", "terms_version", "status"}
                            if table == "programme_routes"
                            else (
                                {
                                    "assessment_status",
                                    "assessment_minute",
                                    "trigger_booking_id",
                                    "terms_version",
                                    "fee_credits_remaining",
                                }
                                if table == "programme_orders"
                                else set()
                            )
                        )
                    )
                    if row["scope"] == c["scope"]
                    else set()
                )
                immutable &= all(current.get(k) == v for k, v in row.items() if k not in allowed)
        check(
            "programme.frame",
            immutable,
            "Original funding attribution, Contract terms, Target and other customer arrangements remain intact. ",
        )
        # Legacy predicates reconstruct service cash from actual settled funding.
        # Programme principal caps, approval, timing and source flows are checked above.
        for index, component in enumerate(c["components"]):
            cc = deepcopy(component)
            cc["initial_world"] = deepcopy(initial)
            ww = deepcopy(world)
            for target in [cc["initial_world"], ww, ww.get("_closure_state")]:
                if target is not None:
                    for table in FACILITY_TABLES:
                        target.pop(table, None)
            for cid in order["capital_by_case"]:
                cc["initial_world"]["service_cases"][cid]["cash_minor"] = sum(
                    t["amount_minor"]
                    for t in transfers
                    if t["destination_case_id"] == cid and t["status"] == "SETTLED"
                )
            result = component_verify(ww, cc)
            for row in result["checks"]:
                checks.append(
                    {
                        **row,
                        "name": f"activity{index+1}:{row['name']}",
                        "description_zh": component.get("title", component["family"])
                        + ": "
                        + row["description_zh"],
                    }
                )
    except (KeyError, TypeError, ValueError) as exc:
        check(
            "programme.well_formed",
            False,
            "Business status lacks required acceptance fields. ",
            str(exc),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed)
