"""Reconstruct event identity, source value and cash without execution helpers."""

from copy import deepcopy


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **kw):
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
        r = deepcopy(old)
        ids = {v["id"]: k for k, v in old.items()}
        terms = old["SOURCE"]["_response"]
        cash = initial["service_cases"][cid]["cash_minor"]
        consumed = initial["service_cases"][cid]["state"]["consumed_quota_minor"]
        withdrawals = []
        valid = True
        reserve = True
        protected = True
        timely = True
        eligible = True
        source_totals = {
            k: sum(
                e["delta_minor"]
                for e in old.values()
                if e["kind"] == "source_event" and e["receivable"] == k
            )
            for k, v in old.items()
            if v["kind"] == "sales_receivable"
        }

        def quota(truth=False):
            return (
                sum(
                    max(
                        0,
                        (source_totals[k] if truth else x["face_minor"])
                        - x["existing_encumbrance_minor"],
                    )
                    for k, x in r.items()
                    if x["kind"] == "sales_receivable"
                    and x["shop"] == terms["target_shop"]
                    and not x["assigned"]
                )
                - consumed
            )

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
            out = {}
            minute = b["minute"]
            if b.get("contract") == "reconciled_sync":
                d = b["arguments"]
                x = r[ids[d["record_id"]]]
                a = b["kind"]
                if a == "grant_stream":
                    x["granted"] = True
                elif a == "import_window":
                    valid &= x["granted"]
                    keys = []
                    for e in sorted(
                        (
                            v
                            for v in old.values()
                            if v["kind"] == "source_event"
                            and v["shop"] == x["shop"]
                            and v["stream"] == x["stream"]
                            and d["after_cursor"] < v["sequence"] <= d["through_cursor"]
                        ),
                        key=lambda v: v["sequence"],
                    ):
                        key = f"{e['native_event']}:{e['revision']}"
                        if key in x["posted_keys"]:
                            continue
                        x["posted_keys"].append(key)
                        keys.append(key)
                        inv = r[e["receivable"]]
                        inv["face_minor"] += e["delta_minor"]
                        if inv["assigned"]:
                            new = max(0, inv["face_minor"]) * inv["advance_bp"] // 10000
                            delta += new - inv["advanced_minor"]
                            inv["advanced_minor"] = new
                    x.update(
                        cursor=max(x["cursor"], d["through_cursor"]),
                        last_attempt_status="COMPLETED",
                    )
                    out = dict(applied_keys=keys, cursor=x["cursor"])
                elif a == "finance":
                    valid &= (
                        not x["assigned"]
                        and x["existing_encumbrance_minor"] == 0
                        and x["face_minor"] > 0
                        and x["advance_bp"] > 0
                    )
                    x.update(
                        assigned=True, advanced_minor=x["face_minor"] * x["advance_bp"] // 10000
                    )
                    delta = x["advanced_minor"]
                elif a == "unwind":
                    valid &= x["assigned"]
                    delta = -x["advanced_minor"]
                    x.update(assigned=False, advanced_minor=0)
                elif a == "withdraw":
                    n = d["amount_minor"]
                    valid &= x["status"] == "VERIFIED" and quota() >= n
                    eligible &= quota(True) >= n
                    net = n * terms["rate"] - terms["cny_fee_minor"]
                    x["paid_minor"] += net
                    consumed += n
                    delta = -n
                    withdrawals.append(
                        dict(
                            recipient=x["reference"],
                            amount_minor=n,
                            net_cny_minor=net,
                            minute=minute,
                        )
                    )
                    out = dict(net_cny_minor=net)
                elif a == "pay_supplier":
                    x["paid_minor"] += d["amount_minor"]
                    delta = -d["amount_minor"]
                    timely &= minute <= x["due_minute"]
                else:
                    valid = False
                valid &= b["receipt"] == out
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "supplier_result",
                "sync_window",
            ):
                ev = w["events"][b["event_id"]]
                valid &= ev["status"] == "COMPLETED" and minute == ev["due_minute"]
                if b["event_kind"] == "supplier_result":
                    x = r["PENDING"]
                    valid &= x["status"] == "PROCESSING"
                    x["status"] = old["PENDING"]["_result"]
                    if x["status"] == "RETURNED":
                        delta = x["amount_minor"] - old["PENDING"]["_return_fee_minor"]
                        x["actual_return_minor"] = delta
                    else:
                        r[x["invoice"]]["paid_minor"] += x["amount_minor"]
                        timely &= minute <= r[x["invoice"]]["due_minute"]
                    out = dict(
                        status=x["status"],
                        actual_return_minor=x["actual_return_minor"],
                        amount_minor=x["amount_minor"],
                    )
                valid &= b["receipt"] == out
            else:
                continue
            cash += delta
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
            protected &= not r[terms["protected_receivable"]]["assigned"]
            valid &= b["cash_delta_minor"] == delta and b["cash_after_minor"] == cash
        wanted_streams = [
            x
            for x in r.values()
            if x["kind"] == "sync_stream" and x["shop"] == terms["target_shop"]
        ]
        coverage = True
        for stream in wanted_streams:
            events = [
                e
                for e in old.values()
                if e["kind"] == "source_event"
                and (e["shop"], e["stream"]) == (stream["shop"], stream["stream"])
            ]
            expected = {f"{e['native_event']}:{e['revision']}" for e in events}
            coverage &= set(stream["posted_keys"]) == expected and len(
                stream["posted_keys"]
            ) == len(expected)
        ck(
            "sync.canonical_complete",
            coverage
            and all(
                x["face_minor"] == source_totals[k]
                for k, x in r.items()
                if x["kind"] == "sales_receivable" and x["shop"] == terms["target_shop"]
            ),
            "Verify actual submission per source event, Amendments and returns; Cursor cannot replace proof of completeness.. ",
            source_totals_minor=source_totals,
        )
        ck(
            "withdrawal.true_allowance",
            eligible and quota(True) >= 0,
            "Each actual FX settlement is based on real net sales, Pledged amount and historical consumption support, Final state leaves no limit gap. ",
            true_final_available_minor=quota(True),
        )
        ck(
            "withdrawal.target_and_amount",
            sum(x["amount_minor"] for x in withdrawals) == terms["withdrawal_minor"]
            and all(x["recipient"] == terms["withdrawal_recipient"] for x in withdrawals),
            "Verify total USD Foreign exchange settlement amount, actual CNY Payee and per-transaction net amount.; Allow lawful partial payments.. ",
            withdrawals=withdrawals,
        )
        ck(
            "supplier.actual_remaining",
            r["SUPPLIER"]["paid_minor"] == r["SUPPLIER"]["amount_minor"]
            and timely
            and r["PENDING"]["status"] != "PROCESSING",
            "Include historical receipts and actual in-transit results, Pay remaining obligations in full without double payment. ",
            actual_paid_minor=r["SUPPLIER"]["paid_minor"],
        )
        ck(
            "finance.protected_receivable",
            protected,
            "Receivables the customer has committed to another business remain unfinanced throughout. ",
        )
        ck(
            "cash.actual_and_reserve",
            cash == w["service_cases"][cid]["cash_minor"]
            and reserve
            and w["service_cases"][cid]["fees_minor"]
            == initial["service_cases"][cid]["fees_minor"],
            "Independently verify financing, Offset, Net refund and actual payment., and ongoing reserve fund. ",
            expected_cash_minor=cash,
        )
        state = dict(
            consumed_quota_minor=consumed, available_quota_minor=quota(), withdrawals=withdrawals
        )
        ck(
            "ledger.independent_reconstruction",
            valid and state == w["service_cases"][cid]["state"],
            "Recalculate all permitted partial transfers and receipts based on actual events, Do not trust aggregated limits. ",
        )
        r["SOURCE"] = deepcopy(now["SOURCE"])
        source_ok = all(
            now["SOURCE"].get(k) == v
            for k, v in old["SOURCE"].items()
            if k not in ("receipt", "receipt_status")
        ) and (not now["SOURCE"].get("receipt") or now["SOURCE"]["receipt"] == terms)
        frame = source_ok and now == r
        for k, x in old.items():
            if x.get("shop") == "OUTLET":
                frame &= now[k] == x
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(rid) == v
                    for rid, v in values.items()
                    if isinstance(v, dict) and v.get("scope") != c["scope"]
                )
        ck(
            "frame.history_and_other_store",
            frame,
            "Retain source event, Correct history, Mirror records and same-name stores not entrusted for processing. ",
        )
        ck(
            "events.completed",
            all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"] and e.get("event_category") == "supplier_result"
            ),
            "The old payment result has actually been generated; The cutoff time marker itself does not require waiting. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close case only once after actual result is completed. ",
        )
        ck(
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
            "Actual operation obtains user approval of bound parameters. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck(
            "state.well_formed",
            False,
            "Missing fields required for independent accounting. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
