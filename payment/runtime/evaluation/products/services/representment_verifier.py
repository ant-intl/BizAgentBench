"""Rebuild each rail independently and compare purchase entitlements, not a gold ordering."""

from copy import deepcopy
from payment.runtime.products.services.recovery_support import finish_audit


def verify(world, c):
    checks = []

    def ck(n, v, z, **e):
        checks.append(dict(name=n, passed=bool(v), description_zh=z, evidence=e))

    try:
        state = world.get("_closure_state") or world
        initial = c["initial_world"]
        cid = c["case_id"]
        rows = {
            r["reference"]: deepcopy(r)
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        ids = {r["id"]: r for r in rows.values()}
        case = deepcopy(initial["service_cases"][cid])
        valid = True
        seen = set()
        refund_times = {}
        books = sorted(
            (
                b
                for k, b in state["service_bookings"].items()
                if k not in initial["service_bookings"] and b.get("case_id") == cid
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in books:
            before = case["cash_minor"]
            receipt = {}
            minute = b["minute"]
            if b.get("contract") == "overlapping_remedies":
                d = b["arguments"]
                p = ids[d["purchase_id"]]
                n = d["amount_minor"]
                valid &= (
                    p["kind"] == "disputed_purchase"
                    and type(n) is int
                    and 0 < n <= p["original_minor"]
                )
                if b["kind"] == "refund":
                    valid &= case["cash_minor"] >= n
                    case["cash_minor"] -= n
                    p["new_refund_minor"] += n
                    refund_times.setdefault(p["reference"], []).append(minute)
                    receipt = dict(purchase_ref=p["reference"], refunded_minor=n)
                elif b["kind"] == "respond":
                    m = ids[d["evidence_id"]]
                    valid &= m["kind"] == "concession_memo" and m["purchase_ref"] == p["reference"]
                    entry = dict(
                        amount_minor=n,
                        evidence_version=m["version"],
                        evidence_status=m["status"],
                        minute=minute,
                    )
                    p["responses"].append(entry)
                    receipt = dict(purchase_ref=p["reference"], accepted=deepcopy(entry))
                else:
                    valid = False
            elif b["kind"] == "bank_event":
                eid = b["event_id"]
                event = initial["events"][eid]
                p = initial["_event_payloads"][eid]
                valid &= eid not in seen and minute == event["due_minute"]
                seen.add(eid)
                out = []
                for ref in p["refs"]:
                    r = rows[ref]
                    valid &= (
                        r["status"] == "PENDING" and r["_actual_outcome"] in r["possible_outcomes"]
                    )
                    r["status"] = r["_actual_outcome"]
                    if r["rail"] == "merchant" and r["status"] == "RETURNED":
                        case["cash_minor"] += r["amount_minor"]
                    out.append(dict(reference=ref, status=r["status"]))
                memo = None
                if p["memo"]:
                    m = rows[p["memo"]]
                    n = m["_actual_concession_minor"]
                    valid &= n in m["possible_concession_minor"]
                    m.update(concession_minor=n, status="FINAL", version="FINAL2")
                    rows[m["purchase_ref"]].update(concession_minor=n, concession_status="FINAL")
                    memo = dict(reference=m["reference"], concession_minor=n, version="FINAL2")
                receipt = dict(remedies=out, memo=memo)
            else:
                valid = False
            valid &= (
                b["receipt"] == receipt
                and b["cash_delta_minor"] == case["cash_minor"] - before
                and b["cash_after_minor"] == case["cash_minor"]
                and b["fee_minor"] == 0
            )
        for p in [x for x in rows.values() if x["kind"] == "disputed_purchase"]:
            ref = p["reference"]
            recovered = (
                sum(
                    x["amount_minor"]
                    for x in rows.values()
                    if x["kind"] == "buyer_remedy"
                    and x["purchase_ref"] == ref
                    and x["status"] == "POSTED"
                )
                + p["new_refund_minor"]
            )
            target = p["concession_minor"]
            m = next(
                x
                for x in rows.values()
                if x["kind"] == "concession_memo" and x["purchase_ref"] == ref
            )
            ck(
                "refund.exact_recovery." + ref,
                p["concession_status"] == "FINAL"
                and recovered == target
                and all(t <= p["refund_due"] for t in refund_times.get(ref, [])),
                "Merge the two refund tracks that actually received funds, Each order exactly meets the final concession. ",
                actual_minor=recovered,
                entitlement_minor=target,
            )
            defended = p["historical_defense_minor"] + sum(
                x["amount_minor"] for x in p["responses"]
            )
            ok = all(
                x["evidence_status"] == "FINAL"
                and x["evidence_version"] == m["version"]
                and x["minute"] <= p["response_deadline"]
                for x in p["responses"]
            )
            ck(
                "response.exact_timely_right." + ref,
                ok and defended == p["original_minor"] - target,
                "Verify valid defenses per order based on actual acceptance receipts and final evidence, Do not treat receipt or duplicate defense as entitlement. ",
                actual_minor=defended,
                entitlement_minor=p["original_minor"] - target,
            )
        return finish_audit(world, c, rows, case, checks, valid, seen)
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        ck(
            "state.well_formed",
            False,
            "Independently verify the required refund, Missing defense or evidence record. ",
            error=str(exc),
        )
        return dict(
            success=False,
            failed=[x["name"] for x in checks if not x["passed"]],
            checks=checks,
            allowed_terminals=["COMPLETED"],
        )
