"""Acceptance reconstructed from issuer facts; no execution or witness imports."""

from copy import deepcopy


def evidence_valid(cert, reliance, accepted_day, notices):
    if cert["status"] != "ISSUED" or accepted_day is None or cert["issued_day"] is None:
        return False
    compatible = (cert["entity"], cert["account"]) == (
        reliance["entity"],
        reliance["account"],
    ) and not (set(reliance["attributes"]) - set(cert["attributes"]))
    compatible &= (
        cert["covers_from_day"] <= reliance["period_from_day"]
        and cert["covers_through_day"] >= reliance["period_through_day"]
    )
    prohibited = False
    for n in notices:
        if n.get("mode") == "WITHDRAWAL":
            prohibited |= (
                n["target_certificate"] == cert["reference"] and accepted_day >= n["effective_day"]
            )
        elif n.get("mode") == "ANNULMENT":
            lo, hi = n["invalid_issue_interval"]
            prohibited |= n["signing_key"] == cert["signing_key"] and lo <= cert["issued_day"] <= hi
    return compatible and not prohibited


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
        byid = {r["id"]: k for k, r in old.items()}
        terms = old["SOURCE"]["_response"]
        notices = []
        for r in old.values():
            if r["kind"] == "issuer_notice":
                n = deepcopy(r)
                if n["status"] == "PENDING":
                    n.update(
                        status="PUBLISHED",
                        mode="ANNULMENT",
                        invalid_issue_interval=n["_actual_interval"],
                    )
                notices.append(n)
        affected = {
            k
            for k, r in old.items()
            if r["kind"] == "evidence_reliance"
            and r["status"] == "COMPLETE"
            and not evidence_valid(old[r["certificate"]], r, r["accepted_day"], notices)
        }
        sim = deepcopy(old)
        cash = initial["service_cases"][cid]["cash_minor"]
        fees = 0
        valid = True
        reserve = True
        pending = []
        seen = set()
        reopen_counts = {}
        rebinds = {}
        bookings = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in bookings:
            delta = 0
            charge = 0
            minute = b["minute"]
            if b.get("contract") == "temporal_certificates":
                d = b["arguments"]
                ref = byid[d["record_id"]]
                r = sim[ref]
                action = b["kind"]
                if action == "order_certificate":
                    valid &= r["status"] == "AVAILABLE"
                    r["status"] = "PENDING"
                    charge = r["fee_minor"]
                    delta = -charge
                    pending.append(("certificate_delivery", ref, minute + r["delivery_delay"]))
                elif action == "reopen_reliance":
                    valid &= r["status"] == "COMPLETE"
                    r.update(
                        status="OPEN",
                        version=r["version"] + 1,
                        reopened=True,
                        accepted_day=None,
                        completed_minute=None,
                    )
                    reopen_counts[ref] = reopen_counts.get(ref, 0) + 1
                elif action == "submit_evidence":
                    cert = sim[byid[d["certificate_id"]]]
                    valid &= r["status"] == "OPEN" and cert["status"] == "ISSUED"
                    r.update(status="PENDING", certificate=cert["reference"])
                    pending.append(("reliance_acceptance", ref, minute + 1))
                elif action == "rebind_authority":
                    valid &= d["versions"] == {
                        sim[k]["id"]: sim[k]["version"] for k in r["requires"]
                    } and all(sim[k]["status"] == "COMPLETE" for k in r["requires"])
                    r.update(
                        bound_versions={k: sim[k]["version"] for k in r["requires"]},
                        rebound=True,
                        rebound_minute=minute,
                    )
                    rebinds[ref] = minute
                else:
                    valid = False
                valid &= b["fee_minor"] == charge
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "issuer_scope",
                "certificate_delivery",
                "reliance_acceptance",
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
                if kind == "issuer_scope":
                    sim[ref].update(
                        status="PUBLISHED",
                        mode="ANNULMENT",
                        invalid_issue_interval=old[ref]["_actual_interval"],
                    )
                elif kind in ("certificate_delivery", "reliance_acceptance"):
                    item = (kind, ref, minute)
                    valid &= item in pending
                    if item in pending:
                        pending.remove(item)
                    if kind == "certificate_delivery":
                        sim[ref].update(
                            status="ISSUED",
                            issued_day=terms["current_day"],
                            delivered_minute=minute,
                        )
                    else:
                        sim[ref].update(
                            status="COMPLETE",
                            accepted_day=terms["current_day"],
                            completed_minute=minute,
                        )
            else:
                continue
            cash += delta
            fees += charge
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
            valid &= b["cash_delta_minor"] == delta and b["cash_after_minor"] == cash
        for ref, r in old.items():
            if r["kind"] != "evidence_reliance":
                continue
            n = now[ref]
            correct = n["status"] == "COMPLETE" and evidence_valid(
                now[n["certificate"]], n, n["accepted_day"], notices
            )
            if r["status"] == "OPEN" or ref in affected:
                correct &= (
                    n["completed_minute"] is not None and n["completed_minute"] <= r["due_minute"]
                )
            else:
                correct &= n == r
            if ref in affected:
                correct &= reopen_counts.get(ref, 0) > 0
            check(
                "reliance.validity_and_scope." + ref,
                correct,
                "Separately by issue date, Factual interval and acceptance date determine probative effect; Only fix genuinely affected historical items. ",
                initially_affected=ref in affected,
                certificate=n["certificate"],
                accepted_day=n["accepted_day"],
                version=n["version"],
            )
        for ref, r in old.items():
            if r["kind"] != "dependent_authority":
                continue
            n = now[ref]
            changed = bool(set(r["requires"]) & affected)
            correct = n["status"] == "ACTIVE" and n["bound_versions"] == {
                k: now[k]["version"] for k in r["requires"]
            }
            if changed:
                correct &= n["rebound"] and rebinds.get(
                    ref, terms["grace_through_minute"] + 1
                ) <= min(r["due_minute"], terms["grace_through_minute"])
            else:
                correct &= n == r
            check(
                "authority.current_versions." + ref,
                correct,
                "Affected downstream permission bindings have completed the newly approved version, Other permissions remain unchanged. ",
                changed_sources=sorted(set(r["requires"]) & affected),
                bound_versions=n["bound_versions"],
            )
        fields = {
            "evidence_certificate": ["status", "issued_day", "delivered_minute"],
            "evidence_reliance": [
                "status",
                "version",
                "reopened",
                "accepted_day",
                "completed_minute",
                "certificate",
            ],
            "dependent_authority": ["bound_versions", "rebound", "rebound_minute"],
            "issuer_notice": ["status", "mode", "invalid_issue_interval"],
        }
        valid &= all(
            all(now[k].get(f) == r.get(f) for f in fields.get(r["kind"], []))
            for k, r in sim.items()
        )
        check(
            "ledger.independent_events",
            valid,
            "Independently rebuild certificate issuance, Submission completed, version progression and fees, Verify all actual statuses. ",
        )
        check(
            "events.final_review",
            not pending
            and all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"] and e.get("event_category") != "marker"
            ),
            "Formal signature scope has been published, Material requests and submissions both have actual receipts; Do not additionally wait for cutoff marker. ",
        )
        check(
            "cash.fees_and_reserve",
            cash == w["service_cases"][cid]["cash_minor"]
            and fees == w["service_cases"][cid]["fees_minor"]
            and fees <= terms["fee_cap_minor"]
            and reserve,
            "Verify actual certificate fees and remaining cash.. ",
            fees_minor=fees,
            cash_minor=cash,
        )
        closing = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closing) == 1
            and closing[0]["result"]["ok"]
            and closing[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all actual fixes and permission bindings. ",
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
            "each application and business change undergoes user approval with bound parameters. ",
        )
        frame = set(now) == set(old) and all(
            all(
                now[k].get(f) == value
                for f, value in r.items()
                if f not in fields.get(r["kind"], [])
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
            "frame.original_records",
            frame,
            "Original entity, Account, Time, Prove attributes and other business records remain unchanged. ",
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
