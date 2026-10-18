"""Independent reconstruction of invoice netting, cash and mandate acceptance."""

from copy import deepcopy


def invoice_excess(rows, credits):
    balances = {k: -r["amount_minor"] for k, r in rows.items() if r["kind"] == "invoice_obligation"}
    for r in rows.values():
        if r["kind"] == "bank_debit":
            balances[rows[r["instruction"]]["obligation"]] += r["amount_minor"]
    for ref, n in credits.items():
        debit = rows[rows[ref]["debit"]]
        balances[rows[debit["instruction"]]["obligation"]] -= n
    return balances


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
        terms = old["SOURCE"]["_response"]
        byid = {r["id"]: k for k, r in old.items()}
        cash = initial["service_cases"][cid]["cash_minor"]
        reserve = True
        valid = True
        credits = {
            k: r["amount_minor"]
            for k, r in old.items()
            if r["kind"] == "bank_credit" and r["status"] == "POSTED"
        }
        queue = {k: r["status"] for k, r in old.items() if r["kind"] == "queued_collection"}
        mandates = {k: None for k, r in old.items() if r["kind"] == "debit_mandate"}
        manual = {k: 0 for k, r in old.items() if r["kind"] == "invoice_obligation"}
        requested = {k: 0 for k, r in old.items() if r["kind"] == "bank_debit"}
        completed = dict(requested)
        new_debits = {}
        requests = []
        deadline_ok = True
        seen_events = set()
        bookings = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in bookings:
            minute = b["minute"]
            delta = 0
            if b.get("contract") == "reconciled_debits":
                d = b["arguments"]
                ref = byid.get(d["record_id"]) or next(
                    k for k, r in new_debits.items() if r["id"] == d["record_id"]
                )
                action = b["kind"]
                if action == "revoke_mandate":
                    valid &= mandates[ref] is None
                    mandates[ref] = minute
                elif action == "recall_collection":
                    valid &= (
                        queue[ref] in ("ACCEPTED", "QUEUED") and minute <= old[ref]["recall_until"]
                    )
                    queue[ref] = "RECALLED"
                elif action == "pay_invoice":
                    manual[ref] += d["amount_minor"]
                    delta = -d["amount_minor"]
                    deadline_ok &= minute <= old[ref]["due_minute"]
                elif action == "return_debit":
                    debit = old.get(ref) or new_debits[ref]
                    amount = d["amount_minor"]
                    available = (
                        debit["amount_minor"]
                        - sum(n for k, n in credits.items() if old[k]["debit"] == ref)
                        - requested.get(ref, 0)
                    )
                    valid &= 0 < amount <= available
                    requested[ref] = requested.get(ref, 0) + amount
                    requests.append((ref, amount, minute))
                else:
                    valid = False
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "mandate_lock",
                "collection",
                "merchant_credit",
                "bank_return",
            ):
                eid = b["event_id"]
                valid &= eid not in seen_events
                seen_events.add(eid)
                event = w["events"][eid]
                p = w["_event_payloads"][eid]
                ref = p["reference"]
                kind = p["event_kind"]
                valid &= minute == event["due_minute"] and event["status"] == "COMPLETED"
                if kind == "mandate_lock":
                    for k, r in old.items():
                        if (
                            r["kind"] == "queued_collection"
                            and queue[k] == "QUEUED"
                            and r["lock_minute"] == minute
                        ):
                            queue[k] = (
                                "ACCEPTED"
                                if mandates[old[r["instruction"]]["mandate"]] is None
                                else "REJECTED"
                            )
                elif kind == "collection":
                    q = old[ref]
                    if queue[ref] == "ACCEPTED":
                        if cash >= q["amount_minor"]:
                            queue[ref] = "SETTLED"
                            delta = -q["amount_minor"]
                            key = "SETTLED_" + ref
                            new_debits[key] = dict(
                                id=c["scope"] + "-" + key,
                                reference=key,
                                kind="bank_debit",
                                instruction=q["instruction"],
                                amount_minor=q["amount_minor"],
                                posted_minute=minute,
                            )
                            requested.setdefault(key, 0)
                            completed.setdefault(key, 0)
                            deadline_ok &= (
                                minute <= old[old[q["instruction"]]["obligation"]]["due_minute"]
                            )
                        else:
                            queue[ref] = "UNPAID"
                    valid &= b["receipt"] == dict(reference=ref, status=queue[ref])
                elif kind == "merchant_credit":
                    credits[ref] = old[ref]["_actual_minor"]
                    delta = credits[ref]
                    valid &= (
                        b["receipt"] == dict(reference=ref, amount_minor=credits[ref])
                        and now[ref]["amount_minor"] == credits[ref]
                        and now[ref]["status"] == "POSTED"
                    )
                elif kind == "bank_return":
                    item = (ref, p["amount_minor"], minute - 1)
                    valid &= item in requests
                    if item in requests:
                        requests.remove(item)
                    completed[ref] = completed.get(ref, 0) + p["amount_minor"]
                    delta = p["amount_minor"]
                    invoice = old[(old.get(ref) or new_debits[ref])["instruction"]]["obligation"]
                    deadline_ok &= minute <= old[invoice]["due_minute"]
            else:
                continue
            cash += delta
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
            valid &= b["cash_delta_minor"] == delta and b["cash_after_minor"] == cash
        allrows = {**old, **new_debits}
        before_returns = invoice_excess(allrows, credits)
        returned = {k: 0 for k in manual}
        for ref, n in completed.items():
            returned[old[allrows[ref]["instruction"]]["obligation"]] += n
        for ref, r in old.items():
            if r["kind"] == "invoice_obligation":
                excess = before_returns[ref]
                check(
                    "invoice.actual_net_and_return." + ref,
                    excess + manual[ref] - returned[ref] == 0
                    and returned[ref] == max(0, excess)
                    and now[ref]["manual_paid_minor"] == manual[ref],
                    "Calculate true net actual payment per original invoice; Refund only the actual excess, Do not offset a refund that should not occur with additional top-up. ",
                    excess_before_service_returns_minor=excess,
                    service_return_minor=returned[ref],
                    manual_topup_minor=manual[ref],
                )
        oldm = terms["retiring_mandate"]
        newm = terms["replacement_mandate"]
        check(
            "mandate.retired_before_window",
            mandates[oldm] is not None
            and mandates[oldm] < terms["retire_by_minute"]
            and now[oldm]["status"] == "REVOKED",
            "old mandate is actually revoked before the next acceptance window, Accepted instructions are handled separately. ",
        )
        check(
            "mandate.replacement_and_others_preserved",
            all(
                minute is None and now[k]["status"] == "ACTIVE"
                for k, minute in mandates.items()
                if k != oldm
            ),
            "Retain substitute mandate and other valid authorizations. ",
            replacement=newm,
        )
        check(
            "events.final_actual_results",
            not requests
            and all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"]
            )
            and w["minute"] >= terms["final_review_minute"]
            and deadline_ok,
            "All credits, Bank refund and normal payment actually completed, And meet the invoice deadline. ",
        )
        check(
            "ledger.independent_reconstruction",
            valid
            and all(now[k]["status"] == v for k, v in queue.items())
            and all(
                now[k]["returned_minor"] == completed.get(k, 0)
                for k, r in allrows.items()
                if r["kind"] == "bank_debit"
            ),
            "Independently rebuild authorization lock., Payment accepted, Cash changes and refund requests, Verify the actual receipt. ",
        )
        check(
            "cash.exact_balance",
            cash == w["service_cases"][cid]["cash_minor"]
            and reserve
            and w["service_cases"][cid]["fees_minor"] == 0,
            "Cash only counts toward new credits and actual payments in this round, Historical credits are not posted twice. ",
            expected_cash_minor=cash,
        )
        closing = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closing) == 1
            and closing[0]["result"]["ok"]
            and closing[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all actual results are completed.. ",
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
            "Each operation obtains user approval with bound parameters. ",
        )
        mutable = {
            "debit_mandate": {"status", "revoked_minute"},
            "queued_collection": {
                "status",
                "recalled_minute",
                "acceptance_minute",
                "settled_minute",
            },
            "bank_debit": {"returned_minor", "return_requests"},
            "bank_credit": {"status", "amount_minor", "posted_minute"},
            "invoice_obligation": {"manual_paid_minor"},
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
        frame &= set(now) - set(old) == set(new_debits) and all(
            all(now[k].get(f) == v for f, v in r.items()) for k, r in new_debits.items()
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
            "Retain original invoice, Clearing, Identity relationships and other business; Only actual events may generate new entries.. ",
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
