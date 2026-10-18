"""Independent fractional allocation and transaction-ledger verification."""

from fractions import Fraction


def expected_amounts(rows, terms, refunds):
    groups = {}
    amounts = {k: 0 for k, v in rows.items() if v["kind"] == "employee"}
    for ref, item in rows.items():
        if item["kind"] != "expense_receipt" or item["receipt_status"] != "FINAL":
            continue
        evidence = [
            v
            for v in rows.values()
            if v["kind"] == "travel_context" and v["booking_reference"] == item["booking_reference"]
        ]
        if len(evidence) != 1 or not evidence[0]["registered_business_purpose"]:
            continue
        typ = item["item_kind"]
        if (
            typ not in ("lodging", "taxi", "meals", "meeting")
            or typ == "meeting"
            and evidence[0]["external_attendees"] <= 0
        ):
            continue
        beneficiary = rows[item["profile"]]["person"]
        key = (beneficiary, item["expense_day"], "meals") if typ == "meals" else (ref, "unlimited")
        for leg in rows.values():
            if (
                leg["kind"] == "funding_leg"
                and leg["receipt"] == ref
                and leg["posting_status"] == "SETTLED"
            ):
                net = leg["amount_minor"] - sum(
                    refunds.get(r["reference"], r["posted_minor"])
                    for r in rows.values()
                    if r["kind"] == "merchant_refund" and r["payment_reference"] == leg["reference"]
                )
                groups.setdefault(key, []).append(
                    (leg["reference"], net, rows[leg["account"]]["owner"])
                )
    for key, items in groups.items():
        gross = sum(v for _, v, _ in items)
        cap = min(gross, terms["meal_cap_minor"]) if len(key) == 3 else gross
        fractions = {
            ref: Fraction(value * cap, gross) if gross else Fraction(0)
            for ref, value, owner in items
        }
        paid = {ref: int(v) for ref, v in fractions.items()}
        remainder = cap - sum(paid.values())
        for ref in sorted(paid, key=lambda k: (-(fractions[k] - paid[k]), k))[:remainder]:
            paid[ref] += 1
        for ref, value, owner in items:
            if owner:
                amounts[owner] += paid[ref]
    return {
        k: n - rows[k]["advance_minor"] - rows[k]["prior_reimbursement_minor"]
        for k, n in amounts.items()
    }


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
        case = w["service_cases"][cid]
        terms = old["SOURCE"]["_response"]
        actual = {
            k: r["_actual_minor"]
            for k, r in old.items()
            if r["kind"] == "merchant_refund" and r["status"] == "PENDING"
        }
        expected = expected_amounts(old, terms, actual)
        payments = {p: [] for p in expected}
        cash = initial["service_cases"][cid]["cash_minor"]
        posted = []
        reserve = True
        for b in sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        ):
            if b.get("contract") == "reconciled_expenses":
                d = b["arguments"]
                person = next(k for k, r in old.items() if r["id"] == d["employee_id"])
                amount = d["amount_minor"]
                signed = amount if b["kind"] == "reimburse" else -amount
                cash -= signed
                payments[person].append((b["minute"], signed, d["bank_version"]))
            elif b["kind"] == "bank_event" and b["event_kind"] == "merchant_refund":
                ref = initial["_event_payloads"][b["event_id"]]["reference"]
                posted.append(ref)
                account = old[old[old[ref]["payment_reference"]]["account"]]
                if account["funding_type"] == "COMPANY":
                    cash += actual[ref]
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
        for person, amount in expected.items():
            ps = payments[person]
            positive = sum(n for _, n, _ in ps if n > 0)
            negative = -sum(n for _, n, _ in ps if n < 0)
            row = now[person]
            ok = (
                positive == row["reimbursed_minor"] == max(0, amount)
                and negative == row["recovered_minor"] == max(0, -amount)
                and all(
                    m <= old[person]["due_minute"] and version == old[person]["bank_version"]
                    for m, n, version in ps
                )
            )
            check(
                "employee.actual_settlement." + person,
                ok,
                "Independently calculate individual remaining balance according to beneficiary limits and actual payment sources, Check actual supplementary payment or recovery and deadline. ",
                expected_signed_minor=amount,
                payments=ps,
            )
        refunds = (
            len(posted) == len(set(posted))
            and set(posted) == set(actual)
            and all(
                now[ref]["status"] == "SETTLED" and now[ref]["posted_minor"] == amount
                for ref, amount in actual.items()
            )
        )
        check(
            "refunds.posted_and_reconciled",
            refunds and w["minute"] >= terms["final_review_minute"],
            "Pending merchant refunds actually processed, Funds attributed based on original payment account. ",
        )
        check(
            "cash.exact_balance",
            case["cash_minor"] == cash and case["fees_minor"] == 0 and reserve,
            "Pay from employee, Recoveries and merchant refunds received by the company rebuild cash; Personal refunds do not enter the company wallet. ",
            expected_cash_minor=cash,
        )
        closing = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closing) == 1
            and closing[0]["result"]["ok"]
            and closing[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all actual settlements are completed. ",
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
            "Each payment and recovery goes through user approval with bound parameters. ",
        )
        mutable = {
            "employee": {"reimbursed_minor", "recovered_minor"},
            "merchant_refund": {"posted_minor", "status"},
            "external_correspondence": {"receipt", "receipt_status"},
        }
        frame = all(
            all(
                now[k].get(field) == value
                for field, value in r.items()
                if field not in mutable.get(r["kind"], set())
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
            "frame.source_facts_and_other_business",
            frame,
            "Retain original receipts., Payment source, Personnel relationships and other business records. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        check(
            "state.well_formed",
            False,
            "Missing original records required for independent accounting. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
