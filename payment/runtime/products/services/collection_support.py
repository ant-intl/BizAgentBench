"""Shared independent ledger, ownership and protocol checks."""

from copy import deepcopy


def protocol(world, c, rs, cash, fees, state, valid, checks):
    w = world.get("_closure_state") or world
    ini = c["initial_world"]
    cid = c["case_id"]
    end = w["service_cases"][cid]

    def ck(n, ok, zh, **kw):
        checks.append(dict(name=n, passed=bool(ok), description_zh=zh, evidence=kw))

    ck(
        "ledger.independent_cash_and_events",
        valid and cash == end["cash_minor"] and fees == end["fees_minor"] and state == end["state"],
        "Independently recalculate each cash transaction, Fees, Actual event and business status. ",
        cash_minor=cash,
        fees_minor=fees,
    )
    immutable = lambda value: {
        k: v for k, v in value.items() if k not in ("cash_minor", "fees_minor", "state")
    }
    ck(
        "frame.case_identity",
        immutable(end) == immutable(ini["service_cases"][cid]),
        "Customer of the business matter., Contract type, Currency, Name and other immutable fields are exactly consistent with the initial state. ",
    )
    ck(
        "frame.business_records",
        rs == {z["reference"]: z for z in w["service_records"].values() if z.get("case_id") == cid},
        "Verify all fields of the business object, Prevent silent tampering with historical facts or unrecorded changes. ",
    )
    frame = True
    for table, values in ini.items():
        if isinstance(values, dict) and not table.startswith("_"):
            outside = lambda source: {
                k: v
                for k, v in source.items()
                if isinstance(v, dict) and v.get("scope") != c["scope"]
            }
            frame &= outside(w.get(table, {})) == outside(values)
    ck(
        "frame.other_businesses",
        frame,
        "Funds for other business, Historical records and relationships remain unchanged.. ",
    )
    closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
    ck(
        "process.single_completion",
        len(closes) == 1
        and closes[0]["result"]["ok"]
        and closes[0]["arguments"]["outcome"] == "COMPLETED",
        "Close case only once after completing all actual business. ",
    )
    audit = world["audit"]

    def approved(q):
        if not (
            type(q.get("approval_seq")) is int
            and type(q.get("executed_seq")) is int
            and q["approval_seq"] < q["executed_seq"]
        ):
            return False
        for a in audit:
            if (
                a.get("actor") != "user"
                or a.get("tool") != "decide_request"
                or a.get("seq") != q["approval_seq"]
                or a.get("arguments") != dict(request_id=q.get("id"), decision="APPROVE")
                or not a.get("result", {}).get("ok")
            ):
                continue
            receipt = a["result"].get("data", {})
            if (
                all(
                    receipt.get(k) == q.get(k)
                    for k in (
                        "id",
                        "operation",
                        "arguments",
                        "scope",
                        "approved_by",
                        "approved_epoch",
                        "approval_seq",
                    )
                )
                and receipt.get("status") == "APPROVED"
            ):
                return True
        return False

    executed = [
        q
        for q in w["requests"].values()
        if q.get("scope") == c["scope"] and q.get("status") == "EXECUTED"
    ]
    ck(
        "process.owner_approval",
        all(approved(q) for q in executed),
        "Parameters actually approved by the user, The permission version and approval sequence are fully bound to the executed request. ",
    )
    contract_tools = {
        "actual_collections": "propose_actual_collection",
        "revised_tolerances": "propose_tolerance_payment",
        "period_coverage": "propose_period_indemnity",
        "contingent_net": "propose_net_delivery",
    }
    new_books = [
        b
        for k, b in w["service_bookings"].items()
        if k not in ini["service_bookings"]
        and b.get("case_id") == cid
        and b.get("contract") == c["kind"]
    ]

    def bound_to_request(b):
        expected = dict(case_id=cid, action=b["kind"], data=b["arguments"])
        for q in executed:
            if (
                q.get("operation") != contract_tools[c["kind"]]
                or q.get("arguments") != expected
                or q.get("receipt") != b
                or not approved(q)
            ):
                continue
            prepared = any(
                a.get("actor") == "assistant"
                and a.get("tool") == "call_discoverable_agent_tool"
                and a.get("seq", q["approval_seq"]) < q["approval_seq"]
                and a.get("arguments") == dict(capability_name=q["operation"], arguments=expected)
                and a.get("result", {}).get("ok")
                and a["result"].get("data", {}).get("id") == q["id"]
                for a in audit
            )
            executed_actual = any(
                a.get("actor") == "assistant"
                and a.get("tool") == "execute_request"
                and a.get("arguments") == dict(request_id=q["id"])
                and a.get("seq") == q["executed_seq"]
                and a.get("minute") == q.get("executed_minute") == b["minute"]
                and a.get("result", {}).get("ok")
                and a["result"].get("data") == b
                for a in audit
            )
            if prepared and executed_actual:
                return True
        return False

    ck(
        "process.booking_approval_binding",
        all(bound_to_request(b) for b in new_books),
        "Each new business ledger entry must correspond to a preparation request with the same parameters, Successful execution receipt with genuine user approval and specified serial number, Isolated ledger entries cannot pass. ",
    )


def outcome(checks):
    failed = [z["name"] for z in checks if not z["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])


def records(c):
    return {
        z["reference"]: deepcopy(z)
        for z in c["initial_world"]["service_records"].values()
        if z.get("case_id") == c["case_id"]
    }


def bookings(w, c):
    return sorted(
        (
            b
            for b in w["service_bookings"].values()
            if b.get("case_id") == c["case_id"]
            and b["id"] not in c["initial_world"]["service_bookings"]
        ),
        key=lambda b: (b["minute"], b["id"]),
    )
