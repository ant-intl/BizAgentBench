"""Independent policy entitlement bounds and claim/payment ledger checks."""

from copy import deepcopy


def permissible(loss, policy):
    dates = (
        (policy["from_day"] <= loss["incident_day"] <= policy["through_day"])
        if policy["basis"] == "OCCURRENCE"
        else (
            policy["from_day"] <= loss["reported_day"] <= policy["through_day"]
            and loss["incident_day"] >= policy["retroactive_day"]
        )
    )
    if (
        not dates
        or loss["reported_day"] > policy["report_through_day"]
        or loss["owner"] != policy["insured"]
        or loss["asset_type"] != policy["asset_type"]
    ):
        return 0
    damage = max(
        0,
        loss["gross_minor"]
        - loss["deductible_minor"]
        - loss["merchant_received_minor"]
        - (loss["salvage_minor"] if loss["salvage_realized"] else 0),
    )
    if policy["layer"] == "EXCESS":
        damage = max(0, damage - policy["attachment_minor"])
    return min(damage, policy["per_loss_cap_minor"])


def verify(world, c):
    checks = []

    def check(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        initial = c["initial_world"]
        w = world.get("_closure_state") or world
        cid = c["case_id"]
        old = {
            r["reference"]: r
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        now = {r["reference"]: r for r in w["service_records"].values() if r.get("case_id") == cid}
        sim = deepcopy(old)
        byid = {r["id"]: k for k, r in old.items()}
        terms = old["SOURCE"]["_response"]
        cash = initial["service_cases"][cid]["cash_minor"]
        reserve = True
        valid = True
        pending = []
        seen = set()
        on_time = True
        for b in sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        ):
            delta = 0
            minute = b["minute"]
            if b.get("contract") == "shared_indemnity":
                d = b["arguments"]
                ref = byid[d["record_id"]]
                r = sim[ref]
                kind = b["kind"]
                if kind == "sell_salvage":
                    valid &= not r["salvage_realized"] and r["salvage_minor"] > 0
                    r["salvage_realized"] = True
                    delta = r["salvage_minor"]
                elif kind == "claim":
                    n = d["amount_minor"]
                    policy = sim[r["policy"]]
                    held = sum(
                        x["requested_minor"] - x["returned_minor"]
                        for x in sim.values()
                        if x["kind"] == "insurance_position" and x["policy"] == r["policy"]
                    )
                    valid &= (
                        n > 0
                        and n + r["requested_minor"] - r["returned_minor"]
                        <= policy["per_loss_cap_minor"]
                        and n + held <= policy["aggregate_cap_minor"]
                    )
                    r["requested_minor"] += n
                    pending.append((ref, n, minute + policy["payout_delay"]))
                elif kind == "return_indemnity":
                    n = d["amount_minor"]
                    valid &= 0 < n <= r["paid_minor"] - r["returned_minor"]
                    r["returned_minor"] += n
                    delta = -n
                elif kind == "pay_replacement":
                    delta = -d["amount_minor"]
                    r["paid_minor"] += d["amount_minor"]
                    on_time &= minute <= r["due_minute"]
                elif kind == "record_recovery":
                    r.update(
                        rights_reconciled=True,
                        declared_external_recovery_minor=d["external_recovery_minor"],
                    )
                else:
                    valid = False
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "merchant_recovery",
                "indemnity_payout",
                "marker",
            ):
                eid = b["event_id"]
                valid &= eid not in seen
                seen.add(eid)
                p = w["_event_payloads"][eid]
                ref = p["reference"]
                kind = p["event_kind"]
                valid &= (
                    w["events"][eid]["due_minute"] == minute
                    and w["events"][eid]["status"] == "COMPLETED"
                )
                if kind == "merchant_recovery":
                    delta = old[ref]["_merchant_actual_minor"]
                    sim[ref]["merchant_received_minor"] += delta
                    valid &= b["receipt"] == dict(reference=ref, received_minor=delta)
                elif kind == "indemnity_payout":
                    item = (ref, p["amount_minor"], minute)
                    valid &= item in pending
                    if item in pending:
                        pending.remove(item)
                    delta = p["amount_minor"]
                    sim[ref]["paid_minor"] += delta
                    valid &= b["receipt"] == dict(reference=ref, paid_minor=delta)
            else:
                continue
            cash += delta
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
            valid &= b["cash_delta_minor"] == delta and b["cash_after_minor"] == cash
        for ref, policy in sim.items():
            if policy["kind"] != "indemnity_policy":
                continue
            positions = [
                p for p in sim.values() if p["kind"] == "insurance_position" and p["policy"] == ref
            ]
            limits = {p["reference"]: permissible(sim[p["loss"]], policy) for p in positions}
            net = {p["reference"]: p["paid_minor"] - p["returned_minor"] for p in positions}
            maximum = min(policy["aggregate_cap_minor"], sum(limits.values()))
            check(
                "policy.retained_indemnity." + ref,
                sum(net.values()) == maximum and all(0 <= n <= limits[k] for k, n in net.items()),
                "By incident and reporting period, Net loss, Insurance layer and shared limit independently calculate final allowed indemnity, Accept equivalent maximum compensation allocation.. ",
                retained_minor=net,
                per_loss_limits_minor=limits,
                maximum_total_minor=maximum,
            )
        for ref, r in sim.items():
            if r["kind"] == "insured_loss":
                realized = r["merchant_received_minor"] + (
                    r["salvage_minor"] if r["salvage_realized"] else 0
                )
                check(
                    "loss.actual_recoveries." + ref,
                    r["rights_reconciled"]
                    and r["declared_external_recovery_minor"] == realized
                    and (r["salvage_minor"] == 0 or r["salvage_realized"])
                    and r["merchant_received_minor"] == r["_merchant_actual_minor"],
                    "Actual residual value and merchant compensation are attributed according to the original loss, And write into the final recovery record. ",
                    external_recovery_minor=realized,
                )
        check(
            "replacement.actual_payments",
            on_time
            and all(
                r["paid_minor"] == r["amount_minor"]
                for r in sim.values()
                if r["kind"] == "replacement_bill"
            ),
            "Each equipment or inventory replacement payment is actually paid in full and on time.. ",
        )
        mutable = {
            "insured_loss": [
                "salvage_realized",
                "merchant_received_minor",
                "rights_reconciled",
                "declared_external_recovery_minor",
            ],
            "insurance_position": ["requested_minor", "paid_minor", "returned_minor"],
            "replacement_bill": ["paid_minor"],
        }
        valid &= all(
            all(now[k].get(f) == r.get(f) for f in mutable.get(r["kind"], []))
            for k, r in sim.items()
        )
        check(
            "ledger.independent_events",
            valid,
            "Independently rebuild advance compensation occupation, Actual receipt, refund release and merchant compensation, Reconcile each cash receipt. ",
        )
        check(
            "cash.exact_balance",
            cash == w["service_cases"][cid]["cash_minor"]
            and w["service_cases"][cid]["fees_minor"] == 0
            and reserve,
            "No funds added out of thin air or cash records offset across insurance layers. ",
            cash_minor=cash,
        )
        check(
            "events.final_results",
            not pending
            and all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"] and e.get("event_category") != "marker"
            ),
            "All advance compensation and merchant compensation have actual outcomes; Do not delay closing for a deadline marker with no state change. ",
        )
        closing = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closing) == 1
            and closing[0]["result"]["ok"]
            and closing[0]["arguments"]["outcome"] == "COMPLETED",
            "Complete all claims, Close only once after payment and recovery records.. ",
        )
        approval = all(
            any(
                a["tool"] == "decide_request"
                and a["arguments"]["request_id"] == q["id"]
                and a["arguments"]["decision"] == "APPROVE"
                and a["result"]["ok"]
                for a in world["audit"]
            )
            and q["approval_seq"] < q["executed_seq"]
            for q in w["requests"].values()
            if q.get("scope") == c["scope"] and q["status"] == "EXECUTED"
        )
        check(
            "process.approval",
            approval,
            "Each operation is approved by a user bound to parameters. ",
        )
        frame = set(old) == set(now) and all(
            all(
                now[k].get(f) == v
                for f, v in r.items()
                if f not in mutable.get(r["kind"], [])
                and not (
                    r["kind"] == "external_correspondence" and f in ("receipt", "receipt_status")
                )
            )
            for k, r in old.items()
        )
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(rid) == r
                    for rid, r in values.items()
                    if isinstance(r, dict) and r.get("scope") != c["scope"]
                )
        check(
            "frame.original_facts",
            frame,
            "Retain insurance period, Impaired assets, Entity and other business records. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        check(
            "state.well_formed",
            False,
            "Missing records required for independent accounting. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
