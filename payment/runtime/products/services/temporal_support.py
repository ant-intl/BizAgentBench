"""Independent temporal execution and business-state checks."""

from copy import deepcopy


def audit_frame(world, c, rows, case, valid, seen, checks):
    state = world.get("_closure_state") or world
    initial = c["initial_world"]
    cid = c["case_id"]
    actual = {
        r["reference"]: r for r in state["service_records"].values() if r.get("case_id") == cid
    }
    checks.append(
        dict(
            name="frame.complete_records_and_cash",
            passed=bool(valid and rows == actual and case == state["service_cases"][cid]),
            description_zh="Independently rebuild all records for this question from per-item operations and events, Funds and fees. ",
            evidence={},
        )
    )
    tables = {
        t
        for t, v in initial.items()
        if isinstance(v, dict) and not t.startswith("_") and t not in ("events", "requests")
    } | {
        t
        for t, v in state.items()
        if isinstance(v, dict) and not t.startswith("_") and t not in ("events", "requests")
    }

    def outside(w):
        return {
            t: {
                k: v
                for k, v in w.get(t, {}).items()
                if isinstance(v, dict) and v.get("scope") != c["scope"]
            }
            for t in tables
        }

    unrelated = outside(state) == outside(initial)
    expected_cases = {
        k: deepcopy(v) for k, v in initial["service_cases"].items() if v.get("scope") == c["scope"]
    }
    expected_cases[cid] = case
    unrelated &= expected_cases == {
        k: v for k, v in state["service_cases"].items() if v.get("scope") == c["scope"]
    }
    checks.append(
        dict(
            name="frame.other_business",
            passed=unrelated,
            description_zh="Other businesses and their historical records remain unchanged.. ",
            evidence={},
        )
    )
    due = {e["id"] for e in initial["events"].values() if e["due_minute"] <= state["minute"]}
    expected_events = deepcopy(initial["events"])
    for eid in seen:
        if eid in expected_events:
            expected_events[eid]["status"] = "COMPLETED"
    events_valid = (
        seen == due
        and expected_events == state["events"]
        and state.get("_event_payloads") == initial.get("_event_payloads")
    )
    checks.append(
        dict(
            name="events.actual_processing",
            passed=events_valid,
            description_zh="All due events have actually been processed, No duplicate or early posting. ",
            evidence={},
        )
    )
    close = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
    checks.append(
        dict(
            name="process.single_completion",
            passed=len(close) == 1
            and close[0]["result"]["ok"]
            and close[0]["arguments"]["outcome"] == "COMPLETED",
            description_zh="Close the case only once after full business completion. ",
            evidence={},
        )
    )
    approved = all(
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
    checks.append(
        dict(
            name="process.owner_approval",
            passed=approved,
            description_zh="Each operation has user approval bound to actual parameters. ",
            evidence={},
        )
    )
    books = [
        b
        for k, b in state["service_bookings"].items()
        if k not in initial["service_bookings"]
        and b.get("case_id") == cid
        and b.get("contract") == c["kind"]
    ]
    requests = [
        q
        for q in state["requests"].values()
        if q.get("scope") == c["scope"] and q.get("status") == "EXECUTED"
    ]

    def bound(book, request):
        args = dict(case_id=cid, action=book["kind"], data=book["arguments"])
        if (
            request.get("receipt") != book
            or request.get("arguments") != args
            or request.get("executed_minute") != book["minute"]
        ):
            return False
        approval_seq = request.get("approval_seq")
        execution_seq = request.get("executed_seq")
        if (
            type(approval_seq) is not int
            or type(execution_seq) is not int
            or approval_seq >= execution_seq
        ):
            return False
        approval = any(
            a.get("actor") == "user"
            and a.get("tool") == "decide_request"
            and a.get("seq") == approval_seq
            and a.get("arguments") == dict(request_id=request["id"], decision="APPROVE")
            and a.get("result", {}).get("ok")
            and a["result"].get("data", {}).get("id") == request["id"]
            and a["result"]["data"].get("status") == "APPROVED"
            and a["result"]["data"].get("arguments") == args
            and a["result"]["data"].get("operation") == request.get("operation")
            for a in world["audit"]
        )
        execution = any(
            a.get("actor") == "assistant"
            and a.get("tool") == "execute_request"
            and a.get("seq") == execution_seq
            and a.get("minute") == book["minute"]
            and a.get("arguments") == dict(request_id=request["id"])
            and a.get("result", {}).get("ok")
            and a["result"].get("data") == book
            for a in world["audit"]
        )
        return approval and execution

    binding = all(sum(bound(book, q) for q in requests) == 1 for book in books) and all(
        sum(bound(book, q) for book in books) == 1 for q in requests
    )
    checks.append(
        dict(
            name="process.booking_approval_binding",
            passed=binding,
            description_zh="Each actual business account entry and request with the same parameters, User actual approvals correspond one-to-one to actual execution receipts, Approval and execution sequence numbers and times are consistent. ",
            evidence=dict(new_business_bookings=len(books), executed_requests=len(requests)),
        )
    )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
