"""Independent commercial cash ledger with physical liquidity for bank events.

The joint projection adds unreceived capital virtually for terminal conservation.
Automatic bank settlement uses the actual funds available before each booking.
Base source SHA256: f4437498b92f7e5daa55600f377db84389d2e18d293e11ebbbcba02623f45903
"""

from copy import deepcopy


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        w = world.get("_closure_state") or world
        ini = c["initial_world"]
        cid = c["case_id"]
        rs = {
            z["reference"]: deepcopy(z)
            for z in ini["service_records"].values()
            if z.get("case_id") == cid
        }
        ids = {z["id"]: ref for ref, z in rs.items()}
        case = ini["service_cases"][cid]
        cash = case["cash_minor"]
        fees = case["fees_minor"]
        guarantees = {}
        valid = True
        directions = True
        events = set()
        payments = {r: [] for r, z in rs.items() if z["kind"] == "import_order"}
        deliveries = {r: [] for r in payments}
        for z in rs.values():
            if z["kind"] == "cash_posting" and z["status"] == "POSTED":
                payments[z["order_ref"]].append(
                    (z["posted_minute"], z["amount_minor"], z["beneficiary_ref"])
                )
        books = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in ini["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in books:
            m = b["minute"]
            before = cash
            fee = 0
            out = {}
            action = b["kind"]
            if b.get("contract") == "documentary_imports":
                d = b["arguments"]
                if action == "direct_pay":
                    o = rs[ids[d["order_id"]]]
                    ben = rs[ids[d["beneficiary_id"]]]
                    n = d["amount_minor"]
                    valid &= (
                        o["kind"] == "import_order"
                        and ben["kind"] == "beneficiary"
                        and type(n) is int
                        and n > 0
                    )
                    cash -= n
                    payments[o["reference"]].append((m, n, ben["reference"]))
                    out = dict(
                        order_ref=o["reference"], beneficiary_ref=ben["reference"], paid_minor=n
                    )
                    allowed = [
                        z["beneficiary_ref"]
                        for z in rs.values()
                        if z["kind"] == "payment_direction"
                        and z["order_ref"] == o["reference"]
                        and z["status"] == "SIGNED"
                        and z["effective_from"] <= m <= z["effective_through"]
                    ]
                    directions &= ben["reference"] in allowed
                elif action == "recall_drawing":
                    z = rs[ids[d["drawing_id"]]]
                    valid &= z["kind"] == "documentary_drawing" and z["status"] == "ACCEPTED"
                    z["status"] = "RECALL_REQUESTED"
                elif action == "settle_confirmed":
                    z = rs[ids[d["drawing_id"]]]
                    valid &= (
                        z["kind"] == "documentary_drawing" and z["status"] == "CONFIRMED_UNPAID"
                    )
                    n = z["actual_minor"]
                    cash -= n
                    z["status"] = "FINAL"
                    payments[z["order_ref"]].append((m, n, z["beneficiary_ref"]))
                    out = dict(
                        order_ref=z["order_ref"], beneficiary_ref=z["beneficiary_ref"], paid_minor=n
                    )
                elif action == "issue_guarantee":
                    o = rs[ids[d["order_id"]]]
                    terms = rs["GUARANTEE_TERMS"]
                    valid &= (
                        o["reference"] not in guarantees and rs[o["title_ref"]]["custody"] == "BANK"
                    )
                    fee = terms["fee_minor"]
                    cash -= fee + terms["collateral_minor"]
                    guarantees[o["reference"]] = dict(
                        status="ACTIVE",
                        collateral_minor=terms["collateral_minor"],
                        issued_minute=m,
                        returned_minute=None,
                    )
                elif action == "lodge_originals":
                    title = rs[ids[d["title_id"]]]
                    valid &= (
                        title["kind"] == "original_title"
                        and title["custody"] == "BANK"
                        and all(
                            z["status"] in ("SETTLED", "RETURNED", "FINAL")
                            for z in rs.values()
                            if z["kind"] == "documentary_drawing"
                            and z["title_ref"] == title["reference"]
                        )
                    )
                    title.update(custody="CARRIER", lodged_minute=m)
                    for ref, g in guarantees.items():
                        if rs[ref]["title_ref"] == title["reference"] and g["status"] == "ACTIVE":
                            cash += g["collateral_minor"]
                            g.update(status="RETURNED", returned_minute=m)
                elif action == "deliver":
                    o = rs[ids[d["order_id"]]]
                    depot = rs[ids[d["depot_id"]]]
                    n = d["quantity_units"]
                    valid &= (
                        o["kind"] == "import_order"
                        and depot["kind"] == "delivery_depot"
                        and type(n) is int
                        and n > 0
                        and (
                            rs[o["title_ref"]]["custody"] == "CARRIER"
                            or guarantees.get(o["reference"], {}).get("status") == "ACTIVE"
                        )
                    )
                    o["delivered_units"] += n
                    valid &= o["delivered_units"] <= o["quantity_units"]
                    deliveries[o["reference"]].append((m, n, depot["reference"]))
                    out = dict(
                        order_ref=o["reference"], depot_ref=depot["reference"], quantity_units=n
                    )
                else:
                    valid = False
                valid &= b["receipt"] == out and b["fee_minor"] == fee
            elif action == "bank_event":
                eid = b["event_id"]
                event = ini["events"][eid]
                payload = ini["_event_payloads"][eid]
                z = rs[payload["drawing_ref"]]
                valid &= (
                    eid not in events
                    and event["due_minute"] == m
                    and w["events"][eid]["status"] == "COMPLETED"
                    and z["status"] in ("ACCEPTED", "RECALL_REQUESTED")
                )
                events.add(eid)
                n = (
                    z["_recalled_actual_minor"]
                    if z["status"] == "RECALL_REQUESTED"
                    else z["_actual_minor"]
                )
                allowed = (
                    z["recall_possible_actual_minor"]
                    if z["status"] == "RECALL_REQUESTED"
                    else z["possible_actual_minor"]
                )
                valid &= n in allowed
                paid = (
                    cash - c.get("_undelivered_capital_before_booking", {}).get(b["id"], 0)
                ) >= n
                z["actual_minor"] = n
                z["status"] = "FINAL" if paid else "CONFIRMED_UNPAID"
                if paid:
                    cash -= n
                    payments[z["order_ref"]].append((m, n, z["beneficiary_ref"]))
                out = dict(
                    drawing_ref=z["reference"],
                    actual_minor=n,
                    paid=paid,
                    beneficiary_ref=z["beneficiary_ref"],
                )
                valid &= b["receipt"] == out and b["event_kind"] == payload["event_kind"]
            else:
                valid = False
            fees += fee
            valid &= (
                cash >= 0
                and b["cash_delta_minor"] == cash - before
                and b["cash_after_minor"] == cash
            )
        for ref, entries in payments.items():
            o = rs[ref]
            target = o["face_minor"] - sum(
                z["amount_minor"]
                for z in rs.values()
                if z["kind"] == "commercial_credit"
                and z["order_ref"] == ref
                and z["invoice_period"] == o["invoice_period"]
                and z["status"] == "SIGNED"
            )
            actual = sum(n for _, n, _ in entries)
            timely = all(m <= o["payment_due_minute"] for m, n, _ in entries if n)
            ck(
                "payment.exact_and_timely." + ref,
                actual == target and timely,
                "Independently reconcile commercial balance according to signed credit items and actual transaction flows, Notification and acceptance descriptions not double counted. ",
                required_minor=target,
                actual_minor=actual,
                receipts=entries,
            )
            moves = deliveries[ref]
            ck(
                "cargo.agreed_delivery." + ref,
                sum(n for _, n, _ in moves) == o["quantity_units"]
                and all(
                    m <= o["delivery_due_minute"] and depot == o["agreed_depot_ref"]
                    for m, n, depot in moves
                ),
                "Verify actual delivery quantity per batch, Agreed warehouse and arrival deadline.. ",
                deliveries=moves,
            )
        ck(
            "payment.current_direction",
            directions,
            "New transfers use the actually effective signed instructions; existing commitments retain the original payee. ",
        )
        ck(
            "security.actual_return",
            all(
                g["status"] == "RETURNED"
                and g["returned_minute"] <= rs["GUARANTEE_TERMS"]["return_due_minute"]
                for g in guarantees.values()
            ),
            "temporary guarantee funds must be actually returned, A bill of lading copy cannot be treated as the insurance cancellation result. ",
            guarantees=guarantees,
        )
        ck(
            "drawings.final_results",
            events == set(ini["events"])
            and all(
                z["status"] in ("FINAL", "SETTLED", "RETURNED")
                for z in rs.values()
                if z["kind"] == "documentary_drawing"
            ),
            "Obtain actual acceptance or withdrawal result, leave no obligation pending deduction. ",
        )
        end = w["service_cases"][cid]
        ck(
            "ledger.cash_and_fees",
            valid
            and cash == end["cash_minor"]
            and fees == end["fees_minor"]
            and fees <= 2500
            and end["state"] == ({"guarantees": guarantees} if guarantees else {}),
            "Independently verify actual payments, Handling fee, Guarantee hold and return, Bank service fee does not exceed customer. USD 25 Cap. ",
            cash_minor=cash,
            fees_minor=fees,
        )
        actual = {
            z["reference"]: z for z in w["service_records"].values() if z.get("case_id") == cid
        }
        frame = rs == actual
        for table, values in ini.items():
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
            "frame.records_preserved",
            frame,
            "Retain the original accounting records, Signing relationship, Bill of lading scope and other business records. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after business is actually completed. ",
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
            "The actual instruction must have user approval with bound parameters.. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck(
            "state.well_formed",
            False,
            "Missing business facts required for independent accounting. ",
            error=str(e),
        )
    failed = [z["name"] for z in checks if not z["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
