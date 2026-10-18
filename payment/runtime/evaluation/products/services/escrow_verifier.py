"""Independent reconstruction of inspection prices, rights and actual cash."""

from copy import deepcopy


def entitlements(rows, lot):
    reports = [
        z
        for z in rows.values()
        if z["kind"] == "acceptance_report"
        and z["lot_ref"] == lot["reference"]
        and z["status"] == "FINAL"
        and z["issued_minute"] is not None
    ]
    if not reports:
        return None
    report = max(reports, key=lambda z: z["revision"])
    prices = report["accepted_prices_minor"]
    units = [
        z for z in rows.values() if z["kind"] == "escrow_unit" and z["lot_ref"] == lot["reference"]
    ]
    assert set(prices) == {z["reference"] for z in units} and all(
        type(prices[z["reference"]]) is int
        and 0 <= prices[z["reference"]] <= z["original_price_minor"]
        for z in units
    )
    gross = sum(prices.values())
    hold = gross * lot["retention_bp"] // 10000
    left = gross - hold
    paid = {}
    for z in sorted(
        (
            z
            for z in rows.values()
            if z["kind"] == "receivable_assignment"
            and z["lot_ref"] == lot["reference"]
            and z["status"] == "EFFECTIVE"
        ),
        key=lambda z: z["priority"],
    ):
        n = (
            min(left, z["cap_minor"], sum(prices[k] for k in z["eligible_unit_refs"]))
            if z["basis"] == "ELIGIBLE_GROSS_CAPPED"
            else left * z["share_bp"] // 10000
        )
        paid[z["party_ref"]] = paid.get(z["party_ref"], 0) + n
        left -= n
    paid[lot["seller_ref"]] = paid.get(lot["seller_ref"], 0) + left
    deposits = sorted(
        (
            z
            for z in rows.values()
            if z["kind"] == "escrow_deposit" and z["lot_ref"] == lot["reference"] and z["posted"]
        ),
        key=lambda z: z["refund_order"],
    )
    refund = lot["original_funded_minor"] - gross
    portions = [refund * z["amount_minor"] // lot["original_funded_minor"] for z in deposits]
    residue = refund - sum(portions)
    for i in range(residue):
        portions[i] += 1
    returned = {}
    for z, n in zip(deposits, portions):
        returned[z["remitter_ref"]] = returned.get(z["remitter_ref"], 0) + n
    return dict(distributions=paid, refunds=returned, retention=hold, report=report["reference"])


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        w = world.get("_closure_state") or world
        initial = c["initial_world"]
        cid = c["case_id"]
        rs = {
            z["reference"]: deepcopy(z)
            for z in initial["service_records"].values()
            if z.get("case_id") == cid
        }
        ids = {z["id"]: k for k, z in rs.items()}
        cash = initial["service_cases"][cid]["cash_minor"]
        valid = True
        on_time = True
        pending = {}
        consumed = set()
        books = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in books:
            start = cash
            out = {}
            m = b["minute"]
            if b.get("contract") == "inspection_escrow":
                d = b["arguments"]
                lot = rs[ids[d["lot_id"]]]
                party = rs[ids[d["recipient_id"]]]
                ref = party["reference"]
                n = d["amount_minor"]
                valid &= (
                    lot["kind"] == "escrow_lot"
                    and party["kind"] == "escrow_party"
                    and type(n) is int
                    and n > 0
                )
                if b["kind"] in ("distribute", "refund_buyer"):
                    lot["available_minor"] -= n
                    cash -= n
                    key = "distributions_minor" if b["kind"] == "distribute" else "refunds_minor"
                    lot[key][ref] = lot[key].get(ref, 0) + n
                    on_time &= m <= lot["due_minute"]
                elif b["kind"] == "return_distribution":
                    outstanding = (
                        lot["distributions_minor"].get(ref, 0)
                        - lot["returned_minor"].get(ref, 0)
                        - sum(
                            p["amount_minor"]
                            for k, p in pending.items()
                            if k not in consumed
                            and p["lot_ref"] == lot["reference"]
                            and p["party_ref"] == ref
                        )
                    )
                    valid &= n <= outstanding
                    eid = b["receipt"]["event_id"]
                    valid &= eid not in pending
                    pending[eid] = dict(
                        event_kind="distribution_return",
                        lot_ref=lot["reference"],
                        party_ref=ref,
                        amount_minor=n,
                    )
                    out = dict(event_id=eid, due_minute=m + 1)
                    valid &= (
                        w["events"][eid]["due_minute"] == m + 1
                        and w["_event_payloads"][eid] == pending[eid]
                    )
                else:
                    valid = False
            elif b["kind"] == "bank_event":
                eid = b["event_id"]
                ev = w["events"][eid]
                valid &= (
                    ev["due_minute"] == m and ev["status"] == "COMPLETED" and eid not in consumed
                )
                consumed.add(eid)
                if b["event_kind"] == "final_inspections":
                    valid &= eid in initial["events"]
                    out = deepcopy(initial["_event_payloads"][eid])
                    for z in rs.values():
                        if z["kind"] == "acceptance_report" and z["status"] == "PENDING":
                            z.update(
                                accepted_prices_minor=deepcopy(z["_actual_prices_minor"]),
                                status=z["_actual_status"],
                                issued_minute=m,
                            )
                elif b["event_kind"] == "distribution_return":
                    out = deepcopy(pending[eid])
                    lot = rs[out["lot_ref"]]
                    ref = out["party_ref"]
                    n = out["amount_minor"]
                    lot["available_minor"] += n
                    cash += n
                    lot["returned_minor"][ref] = lot["returned_minor"].get(ref, 0) + n
                    on_time &= m <= lot["due_minute"]
                else:
                    valid = False
            else:
                valid = False
            valid &= (
                b["receipt"] == out
                and b["cash_delta_minor"] == cash - start
                and b["cash_after_minor"] == cash
                and cash >= 0
                and all(z["available_minor"] >= 0 for z in rs.values() if z["kind"] == "escrow_lot")
            )

        def sparse(d):
            return {k: v for k, v in d.items() if v}

        for lot in (z for z in rs.values() if z["kind"] == "escrow_lot"):
            target = entitlements(rs, lot)
            actual = {
                k: lot["distributions_minor"].get(k, 0) - lot["returned_minor"].get(k, 0)
                for k in set(lot["distributions_minor"]) | set(lot["returned_minor"])
            }
            ck(
                "rights.distributions." + lot["reference"],
                target is not None and sparse(actual) == sparse(target["distributions"]),
                "Recalculate true net payment per payee by final acceptance and assignment order.. ",
                expected=target,
                actual=actual,
            )
            ck(
                "rights.buyer_refunds." + lot["reference"],
                target is not None and sparse(lot["refunds_minor"]) == sparse(target["refunds"]),
                "Verify each buyer’s actual refund according to the original contribution ratio of that batch.. ",
                actual=lot["refunds_minor"],
            )
            ck(
                "retention." + lot["reference"],
                target is not None and lot["available_minor"] == target["retention"],
                "This batch retains the exact warranty retention amount, Other batches must not substitute for its balance. ",
                actual_minor=lot["available_minor"],
            )
        ck(
            "ledger.reconstruction",
            valid
            and cash == w["service_cases"][cid]["cash_minor"]
            and cash == sum(z["available_minor"] for z in rs.values() if z["kind"] == "escrow_lot")
            and w["service_cases"][cid]["fees_minor"] == 0,
            "Independently replay all fund events, Reconcile total cash and cash per escrow batch. ",
        )
        ck(
            "events.actual_and_timely",
            on_time
            and set(pending) <= consumed
            and all(
                eid in consumed
                for eid, e in initial["events"].items()
                if e.get("scope") == c["scope"]
            ),
            "Acceptance and recovery take actual effect, Each payment is completed within its respective deadline. ",
        )
        now = {z["reference"]: z for z in w["service_records"].values() if z.get("case_id") == cid}
        frame = rs == now
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(k) == v
                    for k, v in values.items()
                    if isinstance(v, dict) and v.get("scope") != c["scope"]
                )
        ck(
            "frame.source_and_other_business",
            frame,
            "Acceptance, Original contribution., Assigned contracts and historical payments may only undergo changes defined by public events. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close only once after actual settlement is complete. ",
        )
        ck(
            "process.owner_approval",
            all(
                q["approval_seq"] < q["executed_seq"]
                and any(
                    a["tool"] == "decide_request"
                    and a["arguments"]["request_id"] == q["id"]
                    and a["arguments"]["decision"] == "APPROVE"
                    and a["result"]["ok"]
                    for a in world["audit"]
                )
                for q in w["requests"].values()
                if q.get("scope") == c["scope"] and q["status"] == "EXECUTED"
            ),
            "Each actual instruction has user approval with bound parameters. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration, AssertionError, IndexError) as e:
        ck(
            "state.well_formed",
            False,
            "Missing or violating facts required for independent accounting. ",
            error=str(e),
        )
    failed = [z["name"] for z in checks if not z["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
