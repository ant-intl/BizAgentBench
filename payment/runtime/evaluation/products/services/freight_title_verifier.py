"""Independent title-right, window-capacity and minimum-cost verifier."""

from copy import deepcopy

from payment.runtime.products.services.freight_title_bound import minimum


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
        case = deepcopy(initial["service_cases"][cid])
        cash = case["cash_minor"]
        fees = case["fees_minor"]
        valid = True
        reserve = True
        seen = set()
        moves = []
        books = sorted(
            (
                b
                for b in state["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for booking in books:
            before = cash
            kind = booking["kind"]
            minute = booking["minute"]
            receipt = {}
            charge = 0
            if booking.get("contract") == "document_title_allocation":
                data = booking["arguments"]
                if kind == "activate_split":
                    title = rows[ids[data["title_id"]]]
                    children = [rows[x] for x in title["split_child_refs"]]
                    valid &= (
                        title["status"] == "ACTIVE"
                        and title["endorsed_destination_ref"] is None
                        and bool(children)
                        and all(x["status"] == "PENDING_SPLIT" for x in children)
                    )
                    title["status"] = "RETIRED_BY_SPLIT"
                    for child in children:
                        child["status"] = "ACTIVE"
                    charge = rows["TERMS"]["split_fee_minor"]
                    receipt = dict(
                        retired_title_ref=title["reference"],
                        activated_title_refs=[x["reference"] for x in children],
                    )
                elif kind == "endorse_title":
                    title = rows[ids[data["title_id"]]]
                    destination = rows[ids[data["destination_id"]]]
                    valid &= (
                        title["kind"] == "title_document"
                        and title["status"] == "ACTIVE"
                        and title["endorsed_destination_ref"] is None
                        and destination["kind"] == "cargo_destination"
                    )
                    title["endorsed_destination_ref"] = destination["reference"]
                    charge = rows["TERMS"]["endorsement_fee_minor"]
                    receipt = dict(
                        title_ref=title["reference"],
                        destination_ref=destination["reference"],
                        covers=deepcopy(title["covers"]),
                    )
                elif kind == "dispatch":
                    lot = rows[ids[data["lot_id"]]]
                    window = rows[ids[data["window_id"]]]
                    destination = rows[ids[data["destination_id"]]]
                    candidates = [
                        x
                        for x in rows.values()
                        if x["kind"] == "title_document"
                        and x["status"] == "ACTIVE"
                        and lot["reference"] in x["covers"]
                        and x["endorsed_destination_ref"] == destination["reference"]
                    ]
                    valid &= (
                        not lot["dispatched"]
                        and window["status"] == "OPEN"
                        and window["minute"] == minute
                        and minute == state["service_bookings"][booking["id"]]["minute"]
                        and bool(candidates)
                    )
                    valid &= (
                        window["used_units"] + lot["handling_units"] <= window["capacity_units"]
                    )
                    tariff = rows[lot["tariff_ref"]]
                    d = max(0, minute - tariff["free_through"])
                    storage = lot["handling_units"] * (
                        min(d, 1) * tariff["first_window_rate_minor"]
                        + max(0, d - 1) * tariff["later_window_rate_minor"]
                    )
                    loading = tariff["loading_fees_minor"][str(minute)]
                    charge = storage + loading
                    title = next(
                        (
                            x
                            for x in candidates
                            if x["reference"] == booking["receipt"].get("title_ref")
                        ),
                        candidates[0],
                    )
                    window["used_units"] += lot["handling_units"]
                    lot.update(
                        dispatched=True,
                        dispatched_minute=minute,
                        actual_destination_ref=destination["reference"],
                        title_ref=title["reference"],
                    )
                    receipt = dict(
                        lot_ref=lot["reference"],
                        destination_ref=destination["reference"],
                        title_ref=title["reference"],
                        handling_units=lot["handling_units"],
                        storage_minor=storage,
                        loading_minor=loading,
                    )
                    moves.append(deepcopy(receipt) | {"minute": minute, "charge_minor": charge})
                else:
                    valid = False
                cash -= charge
                fees += charge
            elif kind == "bank_event":
                event = state["events"][booking["event_id"]]
                payload = state["_event_payloads"][booking["event_id"]]
                valid &= (
                    event["status"] == "COMPLETED"
                    and event["due_minute"] == minute
                    and payload["event_kind"] == "window_opening"
                )
                seen.add(event["id"])
                for row in rows.values():
                    if row["kind"] == "pickup_window" and row["status"] == "OPEN":
                        row["status"] = "CLOSED"
                rows[payload["window_ref"]]["status"] = "OPEN"
                case["state"]["active_window_ref"] = payload["window_ref"]
                receipt = dict(opened_window_ref=payload["window_ref"])
            else:
                valid = False
            valid &= (
                booking["receipt"] == receipt
                and booking["fee_minor"] == charge
                and booking["cash_delta_minor"] == cash - before
                and booking["cash_after_minor"] == cash
            )
            reserve &= cash >= c["reserve_minor"]

        lots = [r for r in rows.values() if r["kind"] == "titled_cargo_lot"]
        ck(
            "cargo.correct_title_destination_and_deadline",
            valid
            and all(
                r["dispatched"]
                and r["actual_destination_ref"] == r["expected_destination_ref"]
                and r["dispatched_minute"] <= r["due_minute"]
                for r in lots
            ),
            "Verify current goods ownership batch by batch, Actual destination and pickup deadline. ",
            moves=moves,
        )
        cert = minimum(original)
        fixed = (
            original["TERMS"]["split_fee_minor"] + 3 * original["TERMS"]["endorsement_fee_minor"]
        )
        ck(
            "charges.minimum_complete_plan",
            cert["feasible"] and fees == fixed + cert["minimum_dispatch_fee_minor"],
            "Independently search all window allocations and add necessary split and endorsement fees., Verify the minimum total fee of the complete plan. ",
            actual_fee_minor=fees,
            fixed_document_fee_minor=fixed,
            certificate=cert,
        )
        titles_ok = (
            rows["MASTER_AB"]["status"] == "RETIRED_BY_SPLIT"
            and rows["TITLE_A"]["status"] == rows["TITLE_B"]["status"] == "ACTIVE"
            and rows["TITLE_A"]["endorsed_destination_ref"] == "NORTH"
            and rows["TITLE_B"]["endorsed_destination_ref"] == "SOUTH"
            and rows["TITLE_D"]["endorsed_destination_ref"] == "SOUTH"
        )
        ck(
            "titles.current_scope_preserved",
            titles_ok,
            "Joint cargo rights are split only once, Separately use current files with precise scope; Drafts and similar title of goods remain unchanged.. ",
        )
        case.update(cash_minor=cash, fees_minor=fees)
        ck(
            "ledger.capacity_cash_and_reserve",
            reserve and case == state["service_cases"][cid],
            "Rebuild wallet according to actual window capacity and per-transaction fees, And continuously meet reserve amount.. ",
            cash_minor=cash,
            fees_minor=fees,
        )
        scheduled = {e["id"] for e in state["events"].values() if e.get("scope") == c["scope"]}
        ck(
            "events.all_windows_observed",
            seen == scheduled,
            "Obtain all actual pickup windows, Do not treat scheduled time as already open. ",
        )
        actual = {
            r["reference"]: r for r in state["service_records"].values() if r.get("case_id") == cid
        }
        ck(
            "frame.records_and_other_business",
            rows == actual
            and all(
                state[t].get(k) == v
                for t, values in initial.items()
                if isinstance(values, dict)
                and not t.startswith("_")
                and t not in ("events", "requests")
                for k, v in values.items()
                if isinstance(v, dict) and v.get("scope") != c["scope"]
            ),
            "Retain the rate, Original title history and other business records. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close the case only once after all pickups are complete. ",
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
            "Split, Endorsement and each pickup have customer approval with bound parameters.. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration, AssertionError) as exc:
        ck(
            "state.well_formed",
            False,
            "Lacks independent verification of title to goods., Valid records required for the window or fee. ",
            error=str(exc),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
