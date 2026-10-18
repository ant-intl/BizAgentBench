"""Independent dated order application and cumulative remittance verification."""


def allocations(rows, terms):
    debt = {
        k: r["principal_minor"] - r["historical_paid_minor"]
        for k, r in rows.items()
        if r["kind"] == "attachment_order"
    }
    out = {}
    for wage in sorted(
        (r for r in rows.values() if r["kind"] == "payroll_obligation"),
        key=lambda r: (r["period"], r["reference"]),
    ):
        net = wage["gross_minor"] - wage["tax_minor"]
        remaining = max(0, net - wage["protected_disposable_minor"])
        dest = {k: 0 for k in debt if rows[k]["employee"] == wage["employee"]}
        notice = rows["STAY"]
        eligible = [
            k
            for k in dest
            if not (
                k == notice["order"]
                and notice["_decision"] == "STAY"
                and wage["period"] >= notice["earliest_affected_period"]
            )
        ]
        for category, bp in [
            ("SUPPORT", terms["support_cap_bp"]),
            ("ORDINARY", terms["ordinary_aggregate_cap_bp"]),
        ]:
            limit = net * bp // 10000
            dates = sorted(
                {rows[k]["priority_day"] for k in eligible if rows[k]["category"] == category}
            )
            for date in dates:
                same = sorted(
                    k
                    for k in eligible
                    if rows[k]["category"] == category and rows[k]["priority_day"] == date
                )
                total = sum(max(0, debt[k]) for k in same)
                n = min(remaining, limit, total)
                if n == 0:
                    continue
                amounts = [n * max(0, debt[k]) // total for k in same]
                left = n - sum(amounts)
                for i, k in enumerate(same):
                    if left and amounts[i] < debt[k]:
                        amounts[i] += 1
                        left -= 1
                for k, amount in zip(same, amounts):
                    dest[k] += amount
                    debt[k] -= amount
                remaining -= n
                limit -= n
        out[wage["reference"]] = dict(
            attachments=dest,
            tax_minor=wage["tax_minor"],
            employee_minor=net - sum(dest.values()) + wage["expense_repayment_minor"],
        )
    return out


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
        ids = {r["id"]: k for k, r in old.items()}
        terms = old["SOURCE"]["_response"]
        cash = initial["service_cases"][cid]["cash_minor"]
        valid = True
        reserve = True
        on_time = True
        seen = set()
        notice_published = False
        totals = {
            k: dict(employee_minor=0, tax_minor=0, attachments={})
            for k, r in old.items()
            if r["kind"] == "payroll_obligation"
        }
        orders = {k: 0 for k, r in old.items() if r["kind"] == "attachment_order"}
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
            if b.get("contract") == "dated_payroll":
                d = b["arguments"]
                ref = ids[d["record_id"]]
                r = old[ref]
                n = d["amount_minor"]
                delta = -n
                valid &= type(n) is int and n > 0
                on_time &= minute <= r["due_minute"]
                kind = b["kind"]
                if (
                    kind in ("pay_employee", "pay_attachment")
                    and r["employee"] == old[old["STAY"]["order"]]["employee"]
                    and r["period"] >= old["STAY"]["earliest_affected_period"]
                ):
                    valid &= notice_published
                if kind == "pay_employee":
                    totals[ref]["employee_minor"] += n
                    valid &= d["bank_version"] == old[r["employee"]]["bank_version"]
                elif kind == "pay_tax":
                    totals[ref]["tax_minor"] += n
                elif kind == "pay_attachment":
                    order = ids[d["order_id"]]
                    valid &= old[order]["employee"] == r["employee"]
                    totals[ref]["attachments"][order] = totals[ref]["attachments"].get(order, 0) + n
                    orders[order] += n
                else:
                    valid = False
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "payroll_window",
                "payroll_funding",
                "court_decision",
            ):
                eid = b["event_id"]
                event = w["events"][eid]
                p = w["_event_payloads"][eid]
                valid &= (
                    eid not in seen
                    and event["status"] == "COMPLETED"
                    and minute == event["due_minute"]
                )
                seen.add(eid)
                if p["event_kind"] == "payroll_funding":
                    delta = p["amount_minor"]
                    valid &= b["receipt"] == dict(amount_minor=delta)
                elif p["event_kind"] == "court_decision":
                    notice = old[p["reference"]]
                    decision = notice["_decision"]
                    effect = notice["earliest_affected_period"] if decision == "STAY" else None
                    notice_published = True
                    valid &= (
                        now[p["reference"]]["status"] == "PUBLISHED"
                        and now[p["reference"]]["decision"] == decision
                        and now[p["reference"]]["effective_from_period"] == effect
                        and b["receipt"]
                        == dict(
                            reference=p["reference"],
                            decision=decision,
                            effective_from_period=effect,
                        )
                    )
            else:
                continue
            cash += delta
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
            valid &= cash == b["cash_after_minor"] and delta == b["cash_delta_minor"]
        expected = allocations(old, terms)
        for ref, needed in expected.items():
            got = totals[ref]
            r = now[ref]
            pairs = {k: n for k, n in needed["attachments"].items() if n}
            check(
                "payroll.exact_distribution." + ref,
                got["employee_minor"] == needed["employee_minor"]
                and got["tax_minor"] == needed["tax_minor"]
                and got["attachments"] == pairs
                and r["employee_paid_minor"] == got["employee_minor"]
                and r["tax_paid_minor"] == got["tax_minor"]
                and r["allocations"] == got["attachments"],
                "At full salary for the period, Tax, reimbursement, Claim priority order and period effect independently calculate each payee’s amount due. ",
                expected=needed,
                actual=got,
            )
        check(
            "orders.cumulative_balances",
            all(now[k]["paid_minor"] == n for k, n in orders.items()),
            "Held orders only accumulate actual payments made in the current round, Historical payments are not double-counted.. ",
        )
        check(
            "payments.deadlines_and_notice",
            on_time and valid,
            "Verify payroll deadlines item by item, Valid court decision, Identity, Bank version and actual cash event. ",
        )
        check(
            "cash.conservation",
            cash == w["service_cases"][cid]["cash_minor"]
            and reserve
            and w["service_cases"][cid]["fees_minor"] == 0,
            "Employee, Taxes and actual creditor payments are jointly conserved, Do not use future payroll funds in advance. ",
            expected_cash_minor=cash,
        )
        check(
            "events.final_review",
            all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"] and e.get("event_category") != "payroll_window"
            ),
            "Court and payroll funds already have actual results; After completing two periods of payroll, the case can be closed before the deadline.. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all actual obligations complete. ",
        )
        check(
            "process.approval",
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
            "Every operation has obtained customer approval with matching parameters. ",
        )
        mutable = {
            "attachment_order": {"paid_minor"},
            "payroll_obligation": {"employee_paid_minor", "tax_paid_minor", "allocations"},
            "court_notice": {"status", "decision", "effective_from_period"},
            "external_correspondence": {"receipt", "receipt_status"},
        }
        frame = set(old) == set(now) and all(
            all(now[k].get(f) == v for f, v in r.items() if f not in mutable.get(r["kind"], set()))
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
            "Retain wages, Court order, Historical actual payments, Employee identity and other business. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        check(
            "state.well_formed",
            False,
            "Missing fields required for independent accounting. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
