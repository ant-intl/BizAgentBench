"""Independent physical-presentment, commercial-credit and cash acceptance."""

from copy import deepcopy

from payment.runtime.products.services.presentment_bound import assessment


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
        terms = rows["CARD_TERMS"]
        case = deepcopy(initial["service_cases"][cid])
        cash = {
            r["reference"]: r["cash_minor"]
            for r in rows.values()
            if r["kind"] == "company_cash_account"
        }
        fees = 0
        reserve = True
        valid = True
        seen_events = set()
        claims = {}

        def public(row):
            return {k: deepcopy(v) for k, v in row.items() if not k.startswith("_")}

        def byid(record_id, kind):
            row = rows[ids[record_id]]
            assert row["kind"] == kind
            return row

        actual_rows = {
            r["reference"]: r for r in state["service_records"].values() if r.get("case_id") == cid
        }
        books = sorted(
            (
                b
                for b in state["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for booking in books:
            before = sum(cash.values())
            fee_before = fees
            receipt = {}
            kind = booking["kind"]
            minute = booking["minute"]
            if booking.get("contract") == "late_card_reconciliation":
                data = booking["arguments"]
                if kind == "file_dispute":
                    p = byid(data["presentment_id"], "card_presentment")
                    n = data["amount_minor"]
                    valid &= type(n) is int and 0 < n <= p["amount_minor"] and not p["claim_refs"]
                    cash[terms["payment_account_ref"]] -= terms["dispute_fee_minor"]
                    fees += terms["dispute_fee_minor"]
                    valid &= cash[terms["payment_account_ref"]] >= 0
                    candidates = [
                        r
                        for r in actual_rows.values()
                        if r["kind"] == "card_dispute_claim"
                        and r["presentment_ref"] == p["reference"]
                        and r["filed_minute"] == minute
                    ]
                    assert len(candidates) == 1
                    actual = candidates[0]
                    expected_result = (
                        "REJECTED"
                        if p["reference"] in assessment(original)["supported"]
                        else "APPROVED"
                    )
                    claim = dict(
                        id=actual["id"],
                        reference=actual["reference"],
                        name=actual["name"],
                        kind="card_dispute_claim",
                        scope=c["scope"],
                        case_id=cid,
                        customer_access=True,
                        presentment_ref=p["reference"],
                        amount_minor=n,
                        status="PROCESSING",
                        filed_minute=minute,
                        decision_minute=None,
                        possible_results=["APPROVED", "REJECTED"],
                        _actual_result=expected_result,
                        event_id=actual["event_id"],
                    )
                    rows[claim["reference"]] = claim
                    ids[claim["id"]] = claim["reference"]
                    claims[claim["reference"]] = claim
                    p["claim_refs"].append(claim["reference"])
                    receipt = dict(claim=public(claim))
                elif kind == "pay_card":
                    card = byid(data["card_account_id"], "corporate_card_account")
                    account = byid(data["cash_account_id"], "company_cash_account")
                    n = data["amount_minor"]
                    valid &= (
                        type(n) is int
                        and n > 0
                        and card["status"] == account["status"] == "OPEN"
                        and account["currency"] == card["currency"] == "USD"
                        and n <= card["current_due_minor"]
                        and n <= cash[account["reference"]]
                    )
                    cash[account["reference"]] -= n
                    card["current_due_minor"] -= n
                    card["paid_minor"] += n
                    payment = dict(
                        cash_account_ref=account["reference"], amount_minor=n, minute=minute
                    )
                    card["payments"].append(payment)
                    receipt = dict(
                        card_ref=card["reference"],
                        payment=deepcopy(payment),
                        remaining_due_minor=card["current_due_minor"],
                    )
                else:
                    valid = False
            elif kind == "bank_event":
                payload = state["_event_payloads"][booking["event_id"]]
                seen_events.add(booking["event_id"])
                event = state["events"][booking["event_id"]]
                valid &= (
                    event["status"] == "COMPLETED"
                    and event["due_minute"] == minute
                    and booking["event_kind"] == payload["event_kind"]
                )
                if payload["event_kind"] == "merchant_credit":
                    credit = rows[payload["reference"]]
                    amount = credit["_actual_amount_minor"]
                    credit.update(status="POSTED", amount_minor=amount, posted_minute=minute)
                    card = rows[credit["settling_card_ref"]]
                    valid &= card["current_due_minor"] >= amount
                    card["current_due_minor"] -= amount
                    receipt = dict(
                        credit_ref=credit["reference"],
                        amount_minor=amount,
                        settling_card_ref=card["reference"],
                    )
                elif payload["event_kind"] == "dispute_decision":
                    claim = claims[payload["claim_ref"]]
                    claim.update(status=claim["_actual_result"], decision_minute=minute)
                    if claim["status"] == "APPROVED":
                        card = rows[rows[claim["presentment_ref"]]["card_ref"]]
                        valid &= card["current_due_minor"] >= claim["amount_minor"]
                        card["current_due_minor"] -= claim["amount_minor"]
                    receipt = dict(
                        claim_ref=claim["reference"],
                        status=claim["status"],
                        amount_minor=claim["amount_minor"],
                    )
                else:
                    valid = False
            else:
                valid = False
            case_cash = sum(cash.values())
            valid &= (
                booking["receipt"] == receipt
                and booking["cash_delta_minor"] == case_cash - before
                and booking["fee_minor"] == fees - fee_before
                and booking["cash_after_minor"] == case_cash
            )
            reserve &= (
                cash["OPERATING"] >= c["reserves_minor"]["OPERATING"]
                and cash["TAX_RESERVE"] == original["TAX_RESERVE"]["cash_minor"]
            )

        for ref, n in cash.items():
            rows[ref]["cash_minor"] = n
        current_assessment = assessment(rows)
        actual_claims = {
            rows[q["presentment_ref"]]["reference"]: q["amount_minor"]
            for q in claims.values()
            if q["status"] == "APPROVED"
        }
        ck(
            "claims.exact_unauthorized_physical_presentments",
            actual_claims == current_assessment["claims_minor"]
            and all(q["status"] != "PROCESSING" for q in claims.values()),
            "Per final merchant voucher, Physical clearing trail and posted refunds for the same merchant case, Verify each transaction’s true dispute target and amount.. ",
            expected=current_assessment["claims_minor"],
            actual=actual_claims,
            supported=current_assessment["supported"],
        )
        ck(
            "claims.no_supported_or_rejected_presentment",
            all(
                q["presentment_ref"] not in current_assessment["supported"]
                and q["status"] == "APPROVED"
                for q in claims.values()
            ),
            "True offline purchases and other deductions with final evidence must not enter disputes.; Rejected incorrect applications still retain their fees and records. ",
            claims={
                k: dict(presentment_ref=q["presentment_ref"], status=q["status"])
                for k, q in claims.items()
            },
        )
        ck(
            "fees.exact_and_within_permission",
            fees == len(current_assessment["claims_minor"]) * terms["dispute_fee_minor"]
            and fees <= terms["dispute_fee_cap_minor"],
            "Dispute fees calculated by actual number of submissions, And comply with customer total fee authorization. ",
            actual_fee_minor=fees,
            cap_minor=terms["dispute_fee_cap_minor"],
        )
        cards = {ref: r for ref, r in rows.items() if r["kind"] == "corporate_card_account"}
        payment_ok = all(
            r["current_due_minor"] == 0
            and r["paid_minor"] == sum(p["amount_minor"] for p in r["payments"])
            and all(
                p["cash_account_ref"] == terms["payment_account_ref"]
                and p["minute"] <= r["due_minute"]
                for p in r["payments"]
            )
            for r in cards.values()
        )
        ck(
            "cards.actual_due_source_and_deadline",
            payment_ok,
            "After the actual refund and dispute decision, Based on the card’s actual remaining balance., reconcile all payments against the designated funding account and deadline. ",
            cards={
                ref: dict(
                    current_due_minor=r["current_due_minor"],
                    paid_minor=r["paid_minor"],
                    payments=r["payments"],
                )
                for ref, r in cards.items()
            },
        )
        case["cash_minor"] = sum(cash.values())
        case["fees_minor"] = fees
        ck(
            "cash.continuous_reserve_and_protected_account",
            valid and reserve and state["service_cases"][cid] == case,
            "Independently reconstruct company cash after each expense and card payment; Continuous retention requirement for travel operating account, Tax reserve is not used. ",
            cash_minor=cash,
            reserve_minor=c["reserves_minor"],
        )
        scheduled = {e["id"] for e in state["events"].values() if e.get("scope") == c["scope"]}
        ck(
            "events.all_credit_and_claim_results_observed",
            seen_events == scheduled
            and all(
                e["status"] == "COMPLETED"
                for e in state["events"].values()
                if e.get("scope") == c["scope"]
            ),
            "Obtain actual bank final states for pending merchant refunds and all disputes. ",
            events=sorted(seen_events),
        )

        mutable = {
            "company_cash_account": {"cash_minor"},
            "corporate_card_account": {"current_due_minor", "paid_minor", "payments"},
            "card_presentment": {"claim_refs"},
            "merchant_card_credit": {"amount_minor", "status", "posted_minute"},
        }
        frame = True
        for ref, old in original.items():
            frame &= all(
                rows[ref].get(k) == v
                for k, v in old.items()
                if k not in mutable.get(old["kind"], set())
            )
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
            "frame.original_cards_evidence_and_other_business",
            frame,
            "Retain original deduction, Merchant voucher, Historical refunds and other business; New dispute arises from actual instruction. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all bank results and card payments complete. ",
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
            "Each dispute and card payment has customer approval with bound parameters. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration, AssertionError) as exc:
        ck(
            "state.well_formed",
            False,
            "Missing independent verification of deduction, Refund, valid records required for disputes or cash. ",
            error=str(exc),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
