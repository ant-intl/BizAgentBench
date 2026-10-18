"""Independent bank-evidence, daily-interest, journal and cash acceptance."""

from copy import deepcopy
from payment.runtime.products.services.interest_bound import entitlements


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **evidence):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=evidence))

    try:
        state = world.get("_closure_state") or world
        initial = c["initial_world"]
        cid = c["case_id"]
        original = {
            r["reference"]: deepcopy(r)
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        rows = deepcopy(original)
        ids = {r["id"]: ref for ref, r in rows.items()}
        allocated = set(initial["service_records"])
        seen = set()
        valid = True
        reserves_ok = all(rows[ref]["cash_minor"] >= n for ref, n in c["reserves_minor"].items())
        generated_adjustments = []

        def kind(k):
            return [r for r in rows.values() if r["kind"] == k]

        def obj(data, key, k):
            row = rows[ids[data[key]]]
            assert row["kind"] == k
            return row

        def create(reference, name, k, **values):
            number = len(allocated) + 1
            while "SERVICE_RECORDS-" + format(number, "04") in allocated:
                number += 1
            rid = "SERVICE_RECORDS-" + format(number, "04")
            allocated.add(rid)
            row = dict(
                id=rid,
                scope=c["scope"],
                case_id=cid,
                reference=reference,
                name=name,
                kind=k,
                customer_access=True,
                **values,
            )
            rows[reference] = row
            ids[rid] = reference
            return row

        books = sorted(
            (
                b
                for b in state["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for booking in books:
            before = sum(x["cash_minor"] for x in kind("interest_account"))
            receipt = {}
            minute = booking["minute"]
            if booking.get("contract") == "dated_interest":
                data = booking["arguments"]
                action = booking["kind"]
                if action == "record_value_date":
                    item = obj(data, "item_id", "dated_bank_item")
                    message = obj(data, "bank_message_id", "bank_value_confirmation")
                    day = data["value_day"]
                    valid &= (
                        type(day) is int
                        and 1 <= day <= 31
                        and message["verified_genuine"]
                        and message["status"] == "FINAL"
                    )
                    number = len(kind("economic_date_amendment")) + 1
                    entry = create(
                        "DATE-" + str(number),
                        "Economic-date amendment " + str(number),
                        "economic_date_amendment",
                        item_ref=item["reference"],
                        message_ref=message["reference"],
                        value_day=day,
                        minute=minute,
                        status="POSTED",
                        source="SERVICE_OPERATION",
                    )
                    item["current_economic_day"] = day
                    item["amendment_refs"].append(entry["reference"])
                    receipt = dict(amendment=deepcopy(entry))
                elif action == "post_adjustment":
                    statement = obj(data, "statement_id", "signed_interest_statement")
                    period = obj(data, "period_id", "accounting_period")
                    n = data["amount_minor"]
                    account = rows[statement["account_ref"]]
                    valid &= (
                        type(n) is int
                        and n != 0
                        and period["status"] == "OPEN"
                        and account["status"] == "OPEN"
                        and account["cash_minor"] + n >= 0
                    )
                    account["cash_minor"] += n
                    number = len(kind("interest_cash_adjustment")) + 1
                    entry = create(
                        "ADJUSTMENT-" + str(number),
                        "Current interest cash adjustment " + str(number),
                        "interest_cash_adjustment",
                        account_ref=account["reference"],
                        statement_ref=statement["reference"],
                        period_ref=period["reference"],
                        amount_minor=n,
                        status="POSTED",
                        minute=minute,
                        source="SERVICE_OPERATION",
                        possible_results=[],
                    )
                    generated_adjustments.append(entry["reference"])
                    receipt = dict(adjustment=deepcopy(entry))
                elif action == "pay_accountant":
                    obligation = obj(data, "obligation_id", "accounting_fee_obligation")
                    account = obj(data, "payer_account_id", "interest_account")
                    recipient = obj(data, "recipient_id", "interest_recipient")
                    n = data["amount_minor"]
                    valid &= (
                        type(n) is int
                        and n > 0
                        and account["status"] == "OPEN"
                        and recipient["status"] == "VERIFIED"
                        and account["currency"] == recipient["currency"] == "USD"
                        and account["cash_minor"] >= n
                    )
                    account["cash_minor"] -= n
                    payment = dict(
                        payer_ref=account["reference"],
                        recipient_ref=recipient["reference"],
                        amount_minor=n,
                        minute=minute,
                    )
                    obligation["paid_minor"] += n
                    obligation["payments"].append(payment)
                    receipt = dict(
                        obligation_ref=obligation["reference"], payment=deepcopy(payment)
                    )
                else:
                    valid = False
            elif booking["kind"] == "bank_event":
                event_id = booking["event_id"]
                event = initial["events"][event_id]
                payload = initial["_event_payloads"][event_id]
                valid &= (
                    event_id not in seen
                    and event["due_minute"] == minute
                    and state["events"][event_id]["status"] == "COMPLETED"
                    and booking["event_kind"] == payload["event_kind"] == "refund_result"
                )
                seen.add(event_id)
                results = []
                for row in kind("interest_cash_adjustment"):
                    if row["status"] == "PROCESSING":
                        status = row["_actual_result"]
                        valid &= (
                            status in ("SETTLED", "RETURNED") and row["result_minute"] == minute
                        )
                        row.update(status=status, minute=minute)
                        cash = row["amount_minor"] if status == "SETTLED" else 0
                        rows[row["account_ref"]]["cash_minor"] += cash
                        results.append(
                            dict(
                                adjustment_ref=row["reference"],
                                status=status,
                                actual_cash_minor=cash,
                            )
                        )
                receipt = dict(results=results)
            else:
                valid = False
            cash = sum(x["cash_minor"] for x in kind("interest_account"))
            valid &= (
                booking["receipt"] == receipt
                and booking["cash_delta_minor"] == cash - before
                and booking["cash_after_minor"] == cash
                and booking["fee_minor"] == 0
            )
            reserves_ok &= all(
                rows[ref]["cash_minor"] >= n for ref, n in c["reserves_minor"].items()
            )
        calculation = entitlements(original)
        dates = calculation["bank_dates"]
        date_evidence = {}
        date_ok = True
        for ref, expected in dates.items():
            item = rows[ref]
            ok = item["current_economic_day"] == expected["value_day"]
            if original[ref]["current_economic_day"] != expected["value_day"]:
                if not item["amendment_refs"]:
                    ok = False
                else:
                    entry = rows[item["amendment_refs"][-1]]
                    ok &= (
                        entry["item_ref"] == ref
                        and entry["message_ref"] == expected["message_ref"]
                        and entry["value_day"] == expected["value_day"]
                    )
            date_ok &= ok
            date_evidence[ref] = dict(
                expected=expected,
                current_economic_day=item["current_economic_day"],
                amendment_refs=item["amendment_refs"],
                passed=ok,
            )
        ck(
            "dates.correct_item_and_evidence",
            date_ok,
            "According to original transaction, Historical collection path and final bank confirmation are checked against the economic date; Similar amounts or reused memos cannot replace transaction matching. ",
            items=date_evidence,
        )
        actual = {}
        for row in kind("interest_cash_adjustment"):
            if row["status"] in ("POSTED", "SETTLED"):
                actual[row["statement_ref"]] = (
                    actual.get(row["statement_ref"], 0) + row["amount_minor"]
                )
        amounts = {
            ref: dict(
                entitlement_minor=value["refund_entitlement_minor"],
                actual_signed_adjustments_minor=actual.get(ref, 0),
            )
            for ref, value in calculation["statements"].items()
        }
        ck(
            "interest.exact_facility_adjustment",
            all(
                x["entitlement_minor"] == x["actual_signed_adjustments_minor"]
                for x in amounts.values()
            ),
            "Independently calculate per-account interest refund entitlement from daily joint overdraft balance and tiered rates, Then deduct actual refunds and recoveries.. ",
            statements=amounts,
        )
        current_period = next(
            ref
            for ref, row in original.items()
            if row["kind"] == "accounting_period" and row["current"]
        )
        ck(
            "journals.current_period_only",
            all(rows[ref]["period_ref"] == current_period for ref in generated_adjustments),
            "New interest adjustments are all recorded in the current open period, Closed-book history is not written back. ",
            generated_adjustments=generated_adjustments,
            current_period=current_period,
        )
        payment_ok = True
        payments = {}
        for obligation in kind("accounting_fee_obligation"):
            ok = obligation["paid_minor"] == obligation["amount_minor"] and all(
                p["payer_ref"] == obligation["payer_ref"]
                and p["recipient_ref"] == obligation["recipient_ref"]
                and p["minute"] <= obligation["due_minute"]
                for p in obligation["payments"]
            )
            payment_ok &= ok
            payments[obligation["reference"]] = obligation["payments"]
        ck(
            "payments.accountant_source_recipient_and_deadline",
            payment_ok,
            "Verify item by item the amount actually paid for accounting fees, Payment account, Payees and their respective deadlines. ",
            payments=payments,
        )
        expected_case = deepcopy(initial["service_cases"][cid])
        expected_case["cash_minor"] = sum(x["cash_minor"] for x in kind("interest_account"))
        expected_case["fees_minor"] = 0
        ck(
            "cash.accounts_and_continuous_reserves",
            valid and reserves_ok and state["service_cases"][cid] == expected_case,
            "Re-adjust, Actual refunds and accounting fees reconstruct cash in each account, And check the reserve amount after each status change; Remaining fields of the business item must remain unchanged. ",
            accounts={x["reference"]: x["cash_minor"] for x in kind("interest_account")},
            reserves=c["reserves_minor"],
            expected_case=expected_case,
        )
        ck(
            "events.actual_refund_resolved",
            seen == set(initial["events"])
            and all(x["status"] != "PROCESSING" for x in kind("interest_cash_adjustment")),
            "After obtaining the actual receipt or return result of the pending refund, verify the remaining cash adjustment. ",
            events=sorted(seen),
        )
        immutable = {
            "signed_interest_statement",
            "debit_interest_rate",
            "historical_bank_routing",
            "bank_value_confirmation",
            "interest_refund_advice",
            "interest_recipient",
            "accounting_period",
        }
        mutable_fields = {
            "dated_bank_item": {"current_economic_day", "amendment_refs"},
            "interest_account": {"cash_minor"},
            "interest_cash_adjustment": {"status", "minute"},
            "accounting_fee_obligation": {"paid_minor", "payments"},
        }
        frame = all(
            all(
                rows[ref].get(field) == value
                for field, value in old.items()
                if field not in mutable_fields.get(old["kind"], set())
            )
            for ref, old in original.items()
        ) and all(rows[ref] == old for ref, old in original.items() if old["kind"] in immutable)
        actual_rows = {
            r["reference"]: r for r in state["service_records"].values() if r.get("case_id") == cid
        }
        frame &= rows == actual_rows
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    state[table].get(key) == row
                    for key, row in values.items()
                    if isinstance(row, dict) and row.get("scope") != c["scope"]
                )
        ck(
            "frame.original_principal_closed_books_and_generated_records",
            frame,
            "Retain the original payment principal, Closing interest, Bank originals and other business.; Newly added date and current-period adjustment are both generated by real instructions. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all actual adjustments and payments are complete. ",
        )
        approvals = all(
            q["approval_seq"] < q["executed_seq"]
            and any(
                a["tool"] == "decide_request"
                and a["arguments"]["request_id"] == q["id"]
                and a["arguments"]["decision"] == "APPROVE"
                and a["result"]["ok"]
                for a in world["audit"]
            )
            for q in state["requests"].values()
            if q.get("scope") == c["scope"] and q["status"] == "EXECUTED"
        )
        ck(
            "process.owner_approval",
            approvals,
            "Per-item date., Refund, Both recovery and payment instructions have customer approval with bound parameters.. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration, AssertionError) as exc:
        ck(
            "state.well_formed",
            False,
            "Missing independent accounting date., Valid records required for interest or cash. ",
            error=str(exc),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
