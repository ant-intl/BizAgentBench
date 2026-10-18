"""Independent funding arithmetic and isolated checks of BOTH original contracts.

No engine, author planner, or reference trajectory imports. Component projection
restores the other component to its initial rows, preserving global identifier
allocation. Both components are checked separately, and the joint frame check
covers the complete original world before projection.
"""

from copy import deepcopy
from payment.runtime.evaluation.products.lifecycle.verifier import (
    verify as original_component_verify,
)
from payment.runtime.products.joint_funding.records import TABLES


def component_verify(world, c):
    if c.get("kind") == "continuous_guarantees" and c["id"] in (
        "trade_guarantee",
        "trade_guarantee__baseline",
    ):
        from payment.runtime.evaluation.guarantees.guarantee_verifier import verify as corrected

        return corrected(world, c)
    return original_component_verify(world, c)


def audit_sequences(world, state):
    seq = {}
    for q in state["requests"].values():
        if (
            q.get("status") == "EXECUTED"
            and isinstance(q.get("receipt"), dict)
            and "id" in q["receipt"]
        ):
            seq[q["receipt"]["id"]] = q["executed_seq"]
    for b in state["service_bookings"].values():
        if b.get("kind") == "bank_event":
            options = [
                a["seq"]
                for a in world["audit"]
                if a["tool"] == "wait_for_update"
                and a["result"].get("ok")
                and a["minute"] == b["minute"]
            ]
            if options:
                seq[b["id"]] = min(options)
    return seq


def project(world, c, component):
    initial = c["initial_world"]
    cid = component["case_id"]
    cc = deepcopy(component)
    own_ids = {cid} | {
        rid
        for rows in initial.values()
        if isinstance(rows, dict)
        for rid, r in rows.items()
        if isinstance(r, dict) and r.get("case_id") == cid
    }
    event_ids = {eid for eid, e in initial["events"].items() if e.get("target_id") in own_ids}

    def select(w, start=False):
        out = deepcopy(w)
        out.pop("_closure_state", None)
        for table in TABLES:
            out.pop(table, None)
        for table, rows in list(out.items()):
            if not isinstance(rows, dict) or table.startswith("_"):
                continue
            if table == "events":
                out[table] = {
                    rid: r
                    for rid, r in rows.items()
                    if rid in event_ids or r.get("target_id") in own_ids
                }
                continue
            if table == "requests":
                out[table] = {
                    rid: r
                    for rid, r in rows.items()
                    if r.get("arguments", {}).get("case_id") == cid
                    and r.get("operation") != "propose_joint_capital_delivery"
                }
                continue
            projected = {}
            for rid, r in rows.items():
                if (
                    not isinstance(r, dict)
                    or r.get("scope") != c["scope"]
                    or rid in own_ids
                    or r.get("case_id") == cid
                ):
                    projected[rid] = r
                elif rid in initial.get(table, {}):
                    projected[rid] = deepcopy(initial[table][rid])
            out[table] = projected
        out["_event_payloads"] = {
            eid: p for eid, p in out.get("_event_payloads", {}).items() if eid in out["events"]
        }
        o = initial["joint_agreements"][c["agreement_id"]]
        cap = o["capital_by_case"][cid]
        opening = o["initial_operating_by_case"][cid]
        if start:
            out["service_cases"][cid]["cash_minor"] = cap
            for rid, amount in o.get("capital_by_account", {}).items():
                if out["service_records"][rid].get("case_id") == cid:
                    out["service_records"][rid]["cash_minor"] = amount
        else:
            seq = audit_sequences(world, w)
            fs = [f for f in w["joint_fundings"].values() if f["case_id"] == cid]
            adjustments = {}
            for bid, b in out["service_bookings"].items():
                if (
                    b.get("case_id") != cid
                    or bid in initial["service_bookings"]
                    or not (
                        "cash_after_minor" in b
                        or "usd_after_minor" in b
                        or "wallet_after_minor" in b
                    )
                ):
                    continue
                delivered = sum(
                    f["amount_minor"] for f in fs if seq.get(f["id"], 10**9) < seq.get(bid, -1)
                )
                adjustments[bid] = deepcopy(b)
                virtual = cap - opening - delivered
                if "cash_after_minor" in b:
                    adjustments[bid]["cash_after_minor"] += virtual
                elif "usd_after_minor" in b:
                    adjustments[bid]["usd_after_minor"] += virtual
                else:
                    adjustments[bid]["wallet_after_minor"]["USD"] += virtual
                receipt = adjustments[bid].get("receipt")
                if isinstance(receipt, dict) and isinstance(receipt.get("cash_path_minor"), list):
                    receipt["cash_path_minor"] = [
                        value + virtual for value in receipt["cash_path_minor"]
                    ]

            def normalize(x):
                if isinstance(x, dict):
                    if x.get("id") in adjustments and (
                        "cash_after_minor" in x
                        or "usd_after_minor" in x
                        or "wallet_after_minor" in x
                    ):
                        return deepcopy(adjustments[x["id"]])
                    return {k: normalize(v) for k, v in x.items()}
                if isinstance(x, list):
                    return [normalize(v) for v in x]
                return x

            out = normalize(out)
            out["service_cases"][cid]["cash_minor"] += (
                cap - opening - sum(f["amount_minor"] for f in fs)
            )
            for rid, amount in o.get("capital_by_account", {}).items():
                if out["service_records"][rid].get("case_id") == cid:
                    ds = sum(
                        f["amount_minor"]
                        for f in fs
                        if initial["joint_routes"][f["route_id"]].get("destination_account_id")
                        == rid
                    )
                    out["service_records"][rid]["cash_minor"] += amount - ds
        return out

    cc["initial_world"] = select(initial, True)
    ww = select(world)
    if world.get("_closure_state"):
        ww["_closure_state"] = select(world["_closure_state"])
    return ww, cc


def verify(world, c, *, projector=project, component_verifier=component_verify):
    if not c.get("joint_capital"):
        return component_verifier(world, c)
    state = world.get("_closure_state") or world
    initial = c["initial_world"]
    checks = []

    def ck(n, ok, zh, **e):
        checks.append(dict(name=n, passed=bool(ok), description_zh=zh, evidence=e))

    try:
        o = initial["joint_agreements"][c["agreement_id"]]
        actual_o = state["joint_agreements"][o["id"]]
        sources = initial["joint_sources"]
        balances = {sid: r["balance_minor"] for sid, r in sources.items()}
        fees = 0
        valid = True
        earmarks = True
        version = True
        binding = True
        fundings = list(state["joint_fundings"].values())
        initial_routes = initial["joint_routes"]
        native = [
            b
            for bid, b in state["service_bookings"].items()
            if bid not in initial["service_bookings"]
            and b.get("case_id") == o["assessment_case_id"]
            and b.get("contract") == initial["service_cases"][o["assessment_case_id"]]["kind"]
            and b["kind"] != "bank_event"
        ]
        ids = {b["id"] for b in native}
        triggers = [
            a["seq"]
            for a in world["audit"]
            if a["tool"] == "execute_request"
            and a["result"].get("ok")
            and a["result"].get("data", {}).get("id") in ids
        ]
        trigger = min(triggers) if triggers else None
        seqs = audit_sequences(world, state)
        fundings.sort(key=lambda f: seqs.get(f["id"], 10**9))
        delivered = {cid: 0 for cid in o["capital_by_case"]}
        release_valid = True
        from payment.runtime.products.joint_funding.release_sequence import release_sequence

        releases = {
            sid: (
                release_sequence(initial, state, seqs, s["release_condition"])
                if s.get("release_condition")
                else 0
            )
            for sid, s in sources.items()
        }
        for f in fundings:
            route = initial_routes[f["route_id"]]
            cid = f["case_id"]
            v = f["terms_version"]
            fee = (
                (route["fee_minor"] if v == 1 else route["_assessed_fee_minor"])
                if delivered[cid] == 0
                else 0
            )
            fees += fee
            req = [
                q
                for q in state["requests"].values()
                if q.get("operation") == "propose_joint_capital_delivery"
                and q.get("status") == "EXECUTED"
                and q.get("receipt") == f
            ]
            binding &= len(req) == 1
            if req:
                q = req[0]
                expected = {
                    k: f[k]
                    for k in ("agreement_id", "case_id", "route_id", "allocations", "terms_version")
                }
                binding &= (
                    q["arguments"] == expected
                    and q["approval_seq"] < q["executed_seq"]
                    and any(
                        a["seq"] == q["approval_seq"]
                        and a["actor"] == "user"
                        and a["tool"] == "decide_request"
                        and a["arguments"].get("request_id") == q["id"]
                        and a["arguments"].get("decision") == "APPROVE"
                        and a["result"].get("ok")
                        for a in world["audit"]
                    )
                    and any(
                        a["seq"] == q["executed_seq"]
                        and a["tool"] == "execute_request"
                        and a["result"].get("ok")
                        and a["result"]["data"] == f
                        for a in world["audit"]
                    )
                )
                current = (
                    2
                    if cid != o["assessment_case_id"]
                    and trigger is not None
                    and trigger < q["executed_seq"]
                    else 1
                )
                version &= current == v
            else:
                version = False
            valid &= (
                f["agreement_id"] == o["id"]
                and route["case_id"] == cid
                and sum(f["allocations"].values()) == f["amount_minor"]
                and delivered[cid] + f["amount_minor"]
                <= o["capital_by_case"][cid] - o["initial_operating_by_case"][cid]
                and f["fee_minor"] == fee
                and f["status"] == "SETTLED"
                and f["minute"] <= o["funding_deadline"]
            )
            delivered[cid] += f["amount_minor"]
            for sid, n in f["allocations"].items():
                release_valid &= releases[sid] is not None and releases[sid] < seqs.get(f["id"], -1)
                valid &= type(n) is int and n > 0
                balances[sid] -= n
                earmarks &= cid in sources[sid]["permitted_case_ids"]
            balances[o["fee_source_id"]] -= fee
            valid &= all(n >= 0 for n in balances.values())
        ck(
            "joint.exact_capital",
            valid
            and all(
                delivered[cid] + o["initial_operating_by_case"][cid] == cap
                for cid, cap in o["capital_by_case"].items()
            ),
            "Batch-by-batch deliveries plus original wallet cash total equals the agreed principal, Do not increase funds repeatedly. ",
        )
        if o.get("capital_by_account"):
            exact = all(
                sum(
                    f["amount_minor"]
                    for f in fundings
                    if initial_routes[f["route_id"]].get("destination_account_id") == rid
                )
                == cap
                for rid, cap in o["capital_by_account"].items()
            )
            ck(
                "joint.company_capital",
                exact,
                "Group capital must also be recorded company by company, Group totals cannot replace the designated company’s original capital. ",
            )
        ck(
            "joint.source_ownership",
            earmarks,
            "Each source is used only for the agreed business; Bank-accepted transfers may still violate client fund attribution.. ",
        )
        ck(
            "joint.actual_release",
            release_valid
            and all(
                state["joint_sources"][sid]["status"]
                == ("AVAILABLE" if releases[sid] is not None else "RESTRICTED")
                for sid in sources
            ),
            "Independently pay from the business, Refund and clearing transaction records restore source release timing, Unobserved actual fulfillment cannot be used in advance. ",
            release_sequences=releases,
        )
        ck(
            "joint.funding_conservation",
            all(state["joint_sources"][sid]["balance_minor"] == n for sid, n in balances.items()),
            "Independently rebuild all source balances based on per-transaction principal and quoted fees. ",
            expected=balances,
        )
        ck(
            "joint.fee_and_reserve",
            fees <= o["maximum_delivery_fees_minor"]
            and balances[o["fee_source_id"]] >= o["minimum_uncommitted_cash_minor"],
            "Total cost of both deliveries and remaining shared cash simultaneously comply with the agreement. ",
            fees_minor=fees,
            remaining_minor=balances[o["fee_source_id"]],
        )
        ck(
            "joint.current_quote",
            version,
            "Determine effective quote based on actual purchase instruction and execution sequence number, Old approval does not replace new terms. ",
        )
        ck(
            "joint.bound_funding_requests",
            binding,
            "Each capital delivery corresponds to actual approval and execution receipt with the same parameters.. ",
        )
        allowed_order = {
            "assessment_status",
            "assessment_booking_id",
            "assessment_seq",
            "terms_version",
        }
        stable = lambda r, skip: {k: v for k, v in r.items() if k not in skip}
        expected_order = deepcopy(o)
        if trigger:
            first = next(a["result"]["data"] for a in world["audit"] if a["seq"] == trigger)
            expected_order.update(
                assessment_status="READY",
                assessment_booking_id=first["id"],
                assessment_seq=trigger,
                terms_version=2,
            )
        ck(
            "joint.actual_assessment",
            actual_o == expected_order,
            "Fee updates are triggered by executed procurement transactions., Query, Draft or oral consent does not trigger an update. ",
        )
        frame = True
        for table, rows in initial.items():
            if (
                not isinstance(rows, dict)
                or table.startswith("_")
                or table in ("requests", "events")
            ):
                continue
            for rid, r in rows.items():
                if not isinstance(r, dict):
                    continue
                rr = state.get(table, {}).get(rid)
                if r.get("scope") != c["scope"]:
                    frame &= rr == r
                elif table == "joint_sources":
                    frame &= stable(rr or {}, {"balance_minor", "status"}) == stable(
                        r, {"balance_minor", "status"}
                    )
                elif table == "joint_routes":
                    expected = deepcopy(r)
                    if trigger and r["case_id"] != o["assessment_case_id"]:
                        expected.update(fee_minor=r["_assessed_fee_minor"], terms_version=2)
                    frame &= rr == expected
            for rid, r in state.get(table, {}).items():
                if rid not in rows and isinstance(r, dict) and r.get("scope") != c["scope"]:
                    frame = False
        frame &= set(state["service_cases"]) == set(initial["service_cases"])
        for table in ("joint_agreements", "joint_sources", "joint_routes"):
            frame &= set(state[table]) == set(initial[table])
        for eid, ev in initial["events"].items():
            frame &= (
                stable(state.get("events", {}).get(eid, {}), {"status", "completed_minute"})
                == stable(ev, {"status", "completed_minute"})
                and state.get("_event_payloads", {}).get(eid) == initial["_event_payloads"][eid]
            )
        ck(
            "joint.global_frame",
            frame,
            "Check all external business and original funding terms before component projection., Cannot use projection to conceal changes in other objects. ",
        )
        # The projected native ledgers validate economic deltas independently.
        # Rebuild the REAL wallet path with actual partial-delivery sequence.
        actual_cash = dict(o["initial_operating_by_case"])
        cash_ok = True
        floors = True
        timeline = []
        running_sources = {sid: s["balance_minor"] for sid, s in sources.items()}
        funding_by_id = {f["id"]: f for f in fundings}
        moves = []
        for f in fundings:
            moves.append((seqs.get(f["id"], -1), f["id"], f["case_id"], f["amount_minor"], None))
        for bid, b in state["service_bookings"].items():
            if (
                bid not in initial["service_bookings"]
                and b.get("case_id") in actual_cash
                and ("cash_delta_minor" in b or "usd_delta_minor" in b or "wallet_delta_minor" in b)
            ):
                delta = b.get(
                    "cash_delta_minor",
                    b.get("usd_delta_minor", b.get("wallet_delta_minor", {}).get("USD", 0)),
                )
                after = b.get(
                    "cash_after_minor",
                    b.get("usd_after_minor", b.get("wallet_after_minor", {}).get("USD")),
                )
                moves.append((seqs.get(bid, -1), bid, b["case_id"], delta, after))
        for sq, bid, cid, delta, after in sorted(moves):
            if bid in funding_by_id:
                f = funding_by_id[bid]
                for sid, n in f["allocations"].items():
                    running_sources[sid] -= n
                running_sources[o["fee_source_id"]] -= f["fee_minor"]
            actual_cash[cid] += delta
            for rcid, rr in o.get("reserve_with_earmarked_source", {}).items():
                floors &= (
                    actual_cash[rcid] + running_sources[rr["source_id"]] >= rr["minimum_minor"]
                )
            cash_ok &= (
                sq >= 0 and actual_cash[cid] >= 0 and (after is None or after == actual_cash[cid])
            )
            floors &= actual_cash[cid] >= o["continuous_floor_by_case"][cid]
            timeline.append(dict(seq=sq, record_id=bid, case_id=cid, cash_minor=actual_cash[cid]))
        cash_ok &= all(
            state["service_cases"][cid]["cash_minor"] == n for cid, n in actual_cash.items()
        )
        ck(
            "joint.actual_wallet_path",
            cash_ok,
            "independently recompute each actual receipt and expenditure before original business ledger projection, Using unreleased capital to cover overdrafts is prohibited.. ",
            timeline=timeline,
        )
        ck(
            "joint.continuous_operating_floors",
            floors,
            "Each actual disbursement, Maintain the publicly agreed continuous reserve after payment and bank events. ",
        )
        for j, component in enumerate(c["components"], 1):
            ww, cc = projector(world, c, component)
            result = component_verifier(ww, cc)
            for row in result["checks"]:
                checks.append(
                    {
                        **row,
                        "name": f"activity{j}:" + row["name"],
                        "description_zh": component.get("title", component["family"])
                        + ": "
                        + row.get("description_zh", ""),
                    }
                )
        argument_rejections = {
            "INVALID_ARGUMENTS",
            "INVALID_ARGUMENT_TYPE",
            "INVALID_ARGUMENT_VALUE",
            "INVALID_OUTCOME",
            "SUMMARY_REQUIRED",
        }
        closure_calls = [
            row
            for row in world.get("audit", [])
            if row.get("actor") == "assistant"
            and row.get("tool") == "record_case_outcome"
            and row.get("result", {}).get("error") not in argument_rejections
        ]
        outcome = world.get("case_outcome")
        ck(
            "joint.single_completed_outcome",
            len(closure_calls) == 1
            and closure_calls[0].get("result", {}).get("ok")
            and isinstance(outcome, dict)
            and outcome.get("outcome") == "COMPLETED"
            and bool(world.get("_closure_state")),
            "Joint funding task must be closed exactly once after all real business is completed. ",
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        ck(
            "joint.well_formed",
            False,
            "Complete state and associations required for independent verification exist. ",
            error=str(exc),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
