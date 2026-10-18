"""Independent allocations and event-ledger acceptance. No execution/solver imports."""

from itertools import product
from copy import deepcopy
from functools import lru_cache
import json
from payment.runtime.evaluation.products.funding.verifier import verify as previous


def minimum(rows, terms, cash, marks, forced=None):
    return deepcopy(
        _minimum_cached(
            json.dumps([rows, terms, cash, marks, forced], sort_keys=True, separators=(",", ":"))
        )
    )


@lru_cache(maxsize=128)
def _minimum_cached(key):
    rows, terms, cash, marks, forced = json.loads(key)
    forced = forced or {}
    loans = {r["reference"]: r for r in rows.values() if r["kind"] == "loan"}
    securities = [a for a in rows.values() if a["kind"] == "asset" and not a.get("delivery_order")]
    eligible = {}
    for a in securities:
        if a["protected"]:
            eligible[a["reference"]] = (a["pledged_to"],)
            continue
        dest = [None]
        for ref, l in loans.items():
            if (
                a["currency"] == l["currency"]
                and a["owner"] == l["borrower"]
                and a["security_class"] in l["accepted_classes"]
                and a["eligible_from"] <= 5 <= a["eligible_through"]
                and (a["status"] == "SETTLED" or a["reference"] in marks)
            ):
                dest.append(ref)
        eligible[a["reference"]] = tuple(dest)
    bound = None
    best = []
    count = 0
    for dest in product(*(eligible[a["reference"]] for a in securities)):
        count += 1
        groups = {k: {} for k in loans}
        for a, d in zip(securities, dest):
            if d:
                issuer = a.get("issuer", a["id"])
                groups[d][issuer] = (
                    groups[d].get(issuer, 0)
                    + marks.get(a["reference"], a["face_minor"])
                    * (10000 - a["haircut_bp"])
                    // 10000
                )
        coverage = {
            k: sum(
                min(v, loans[k].get("issuer_caps_minor", {}).get(issuer, v))
                for issuer, v in group.items()
            )
            for k, group in groups.items()
        }
        step = terms["repayment_increment_minor"]
        repay = {
            k: max(
                forced.get(k, 0),
                ((max(0, l["debt_minor"] - coverage[k]) + step - 1) // step) * step,
            )
            for k, l in loans.items()
        }
        if any(repay[k] > loans[k]["debt_minor"] for k in loans):
            continue
        amount = sum(repay.values())
        fees = sum(l["repayment_fee_minor"] for k, l in loans.items() if repay[k])
        bills = sum(
            r["amount_minor"] - r.get("paid_minor", 0) for r in rows.values() if r["kind"] == "bill"
        )
        if (
            amount
            + fees
            + bills
            + terms["scheduled_debit_minor"]
            + terms["minimum_cash_reserve_minor"]
            > cash
            or fees > terms["repayment_fee_cap_minor"]
        ):
            continue
        value = (amount, fees)
        if bound is None or value < bound:
            bound = value
            best = []
        if value == bound:
            best.append(
                dict(
                    repayment=repay, placement=dict(zip([a["reference"] for a in securities], dest))
                )
            )
    return dict(
        optimum=list(bound) if bound else None,
        assignments_examined=count,
        solutions=best,
        scope="All whole-asset placements and minimum increment repayments; closed-form monotone minimum for each placement. Forced irreversible repayments included when supplied.",
    )


def verify(world, c):
    from payment.runtime.products.services.mechanisms import BY_KIND

    if c.get("kind") in BY_KIND and c["kind"] != "dated_collateral":
        from importlib import import_module

        name = BY_KIND[c["kind"]].implementation_prefix + "_verifier"
        return import_module("." + name, __package__).verify(world, c)
    if c.get("kind") != "dated_collateral":
        return previous(world, c)
    checks = []

    def check(name, ok, zh, **evidence):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=evidence))

    try:
        initial = c["initial_world"]
        cid = c["case_id"]
        state = world.get("_closure_state") or world
        start = initial["service_cases"][cid]
        case = state["service_cases"][cid]
        original = {
            r["reference"]: r
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        current = {
            r["reference"]: r for r in state["service_records"].values() if r.get("case_id") == cid
        }
        terms = original["SOURCE"]["_response"]
        sim = deepcopy(original)
        marks = {
            r["reference"]: r["_settled_value_minor"]
            for r in original.values()
            if r["kind"] == "asset" and r["status"] == "PENDING"
        }
        bound = minimum(original, terms, start["cash_minor"], marks)
        books = sorted(
            (
                b
                for b in state["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        cash = start["cash_minor"]
        fees = 0
        principal = 0
        cover_ok = True
        reserve_ok = True
        bank_debits = []
        repay = {k: 0 for k, r in original.items() if r["kind"] == "loan"}
        payment_log = {k: [] for k, r in original.items() if r["kind"] == "bill"}
        for b in books:
            action = b["kind"]
            d = b.get("arguments", {})
            minute = b["minute"]
            if b.get("contract") == "dated_collateral":
                r = sim[next(k for k, v in original.items() if v["id"] == d["record_id"])]
                if action == "repay":
                    n = d["amount_minor"]
                    principal += n
                    cash -= n
                    r["debt_minor"] -= n
                    if not repay[r["reference"]]:
                        fees += r["repayment_fee_minor"]
                        cash -= r["repayment_fee_minor"]
                    repay[r["reference"]] += n
                elif action == "pledge":
                    r["pledged_to"] = next(
                        k for k, v in original.items() if v["id"] == d["loan_id"]
                    )
                elif action == "release":
                    r["pledged_to"] = None
                    if r.get("delivery_order"):
                        r.update(delivered=True, delivered_minute=minute)
                elif action == "pay_bill":
                    cash -= d["amount_minor"]
                    r["paid_minor"] += d["amount_minor"]
                    payment_log[r["reference"]].append((minute, d["amount_minor"]))
            elif action == "bank_event":
                if b["event_kind"] == "custody_settlement":
                    e = initial["_event_payloads"][b["event_id"]]
                    r = sim[e["reference"]]
                    r.update(status="SETTLED", face_minor=marks[r["reference"]])
                elif b["event_kind"] == "automatic_debit":
                    amount = initial["_event_payloads"][b["event_id"]]["amount_minor"]
                    ok = cash >= amount
                    bank_debits.append((minute, ok, amount))
                    cash -= amount if ok else 0
            for loan in (r for r in sim.values() if r["kind"] == "loan"):
                buckets = {}
                for a in sim.values():
                    if (
                        a["kind"] == "asset"
                        and a["pledged_to"] == loan["reference"]
                        and a["status"] == "SETTLED"
                        and a["owner"] == loan["borrower"]
                        and a["currency"] == loan["currency"]
                        and a["security_class"] in loan["accepted_classes"]
                        and a["eligible_from"] <= minute <= a["eligible_through"]
                    ):
                        issuer = a.get("issuer", a["id"])
                        buckets[issuer] = (
                            buckets.get(issuer, 0)
                            + a["face_minor"] * (10000 - a["haircut_bp"]) // 10000
                        )
                value = sum(
                    min(n, loan.get("issuer_caps_minor", {}).get(issuer, n))
                    for issuer, n in buckets.items()
                )
                cover_ok &= value >= loan["debt_minor"] >= 0
            reserve_ok &= cash >= terms["minimum_cash_reserve_minor"]
        check(
            "allocation.minimum_principal_and_fee",
            bound["optimum"] == [principal, fees],
            "Independently enumerate to find the minimum necessary principal repayment satisfying each obligation, then compare fixed fees; Accept parallel options. ",
            actual=[principal, fees],
            optimum=bound["optimum"],
            assignments_examined=bound["assignments_examined"],
        )
        check(
            "collateral.continuous_coverage",
            cover_ok,
            "After actual business and bank events, check effective collateral coverage loan by loan. ",
        )
        delivery = True
        protected = True
        row_match = True
        for ref, r in original.items():
            if r["kind"] == "asset":
                if r.get("delivery_order"):
                    delivery &= (
                        bool(sim[ref].get("delivered"))
                        and sim[ref].get("delivered_minute", 999)
                        <= r["delivery_order"]["due_minute"]
                        and sim[ref]["pledged_to"] is None
                    )
                if r["protected"]:
                    protected &= sim[ref]["pledged_to"] == r["pledged_to"] and not any(
                        b.get("contract") == "dated_collateral"
                        and b["kind"] == "release"
                        and b["arguments"]["record_id"] == r["id"]
                        for b in books
                    )
                row_match &= all(
                    current[ref].get(k) == sim[ref].get(k)
                    for k in ("pledged_to", "face_minor", "status", "delivered", "delivered_minute")
                )
            elif r["kind"] == "loan":
                row_match &= current[ref]["debt_minor"] == sim[ref]["debt_minor"]
        check(
            "delivery.dates_and_retained_assets",
            delivery and protected,
            "Each external securities delivery is actually completed within its respective deadline, Specified retained safeguard was not released.. ",
        )
        check(
            "cash.posted_state",
            row_match and case["cash_minor"] == cash and case["fees_minor"] == fees,
            "Rebuild assets per actual instructions and original fees, Loan and cash status. ",
            expected_cash_minor=cash,
            actual_cash_minor=case["cash_minor"],
        )
        check(
            "cash.future_debit_and_continuous_reserve",
            reserve_ok
            and bank_debits == [(5, True, terms["scheduled_debit_minor"])]
            and state["minute"] >= 5,
            "Actual performance has scheduled the deduction, And retain reserve funds at each relevant point in time. ",
        )
        bill_ok = all(
            sim[k]["paid_minor"] == r["amount_minor"]
            and current[k]["paid_minor"] == r["amount_minor"]
            and all(m <= r["due_minute"] for m, n in payment_log[k])
            for k, r in original.items()
            if r["kind"] == "bill"
        )
        check(
            "payments.actual_obligations",
            bill_ok,
            "For each invoice independently verify full actual payment and payment deadline.. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all objectives are completed. ",
        )
        approval = True
        for q in state["requests"].values():
            if q.get("scope") == c["scope"] and q["status"] == "EXECUTED":
                approval &= (
                    any(
                        a["tool"] == "decide_request"
                        and a["arguments"]["request_id"] == q["id"]
                        and a["arguments"]["decision"] == "APPROVE"
                        and a["result"]["ok"]
                        for a in world["audit"]
                    )
                    and q["approval_seq"] < q["executed_seq"]
                )
        check(
            "process.approval",
            approval,
            "Every actual execution has user approval bound to this request parameter. ",
        )
        frame = True
        mutable = {
            "debt_minor",
            "repayment_fee_paid",
            "pledged_to",
            "delivered",
            "delivered_minute",
            "face_minor",
            "status",
            "paid_minor",
            "receipt",
            "receipt_status",
        }
        for table, values in initial.items():
            if table.startswith("_") or table in (
                "audit",
                "unlocked",
                "case_outcome",
                "minute",
                "events",
                "requests",
            ):
                continue
            if isinstance(values, dict):
                for rid, row in values.items():
                    if isinstance(row, dict) and row.get("scope") != c["scope"]:
                        frame &= state[table].get(rid) == row
        for ref, r in original.items():
            frame &= all(current[ref].get(k) == v for k, v in r.items() if k not in mutable)
        check(
            "frame.original_facts_and_other_business",
            frame,
            "Original bank facts, Identity, Contract terms and other business records remain complete. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        check(
            "state.well_formed",
            False,
            "Missing the state or event required for independent acceptance. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
