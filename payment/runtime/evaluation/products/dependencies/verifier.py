"""Independent native-component and cross-process link verification."""

from copy import deepcopy

from payment.runtime.evaluation.products.lifecycle.verifier import verify as native_verify


def cash_delta(record):
    if not isinstance(record, dict):
        return 0
    for key in ("cash_delta_minor", "usd_delta_minor"):
        if isinstance(record.get(key), int):
            return record[key]
    wallet = record.get("wallet_delta_minor")
    return wallet.get("USD", 0) if isinstance(wallet, dict) else 0


def request_case(state, request_id):
    return state.get("requests", {}).get(request_id, {}).get("arguments", {}).get("case_id")


def milestones(world, contract, case_id):
    state = world.get("_closure_state") or world
    initial = contract["initial_world"]
    proposals, approvals, executions, credits, debits, events = [], [], [], [], [], []
    seen_executions = set()
    for row in world.get("audit", []):
        if not row.get("result", {}).get("ok"):
            continue
        if row["tool"] == "call_discoverable_agent_tool":
            request = row["result"].get("data", {})
            if request.get("arguments", {}).get("case_id") == case_id:
                proposals.append(row["seq"])
        elif row["tool"] == "decide_request":
            if (
                row["arguments"].get("decision") == "APPROVE"
                and request_case(state, row["arguments"].get("request_id")) == case_id
            ):
                approvals.append(row["seq"])
        elif row["tool"] == "execute_request":
            data = row["result"].get("data", {})
            if data.get("case_id") == case_id:
                request_id = row["arguments"].get("request_id")
                request = state.get("requests", {}).get(request_id, {})
                if request_id in seen_executions or (
                    request.get("executed_seq") is not None
                    and request["executed_seq"] != row["seq"]
                ):
                    continue
                seen_executions.add(request_id)
                executions.append(row["seq"])
                delta = cash_delta(data)
                if delta > 0:
                    credits.append(row["seq"])
                elif delta < 0:
                    debits.append(row["seq"])
        elif row["tool"] == "wait_for_update":
            minute = row["minute"]
            if any(
                booking_id not in initial.get("service_bookings", {})
                and booking.get("case_id") == case_id
                and booking.get("kind") == "bank_event"
                and booking.get("minute") == minute
                for booking_id, booking in state.get("service_bookings", {}).items()
            ):
                events.append(row["seq"])
    return dict(
        proposals=proposals,
        approvals=approvals,
        executions=executions,
        credits=credits,
        debits=debits,
        events=events,
    )


def relation_value(relation, primary, secondary):
    def first(values):
        return min(values) if values else None

    def last(values):
        return max(values) if values else None

    values = {
        "event_before_secondary_execution": (
            first(primary["events"]),
            first(secondary["executions"]),
        ),
        "primary_execution_before_secondary_approval": (
            first(primary["executions"]),
            first(secondary["approvals"]),
        ),
        "primary_credit_before_secondary_debit": (
            first(primary["credits"]),
            first(secondary["debits"]),
        ),
        "primary_debit_before_secondary_debit": (
            first(primary["debits"]),
            first(secondary["debits"]),
        ),
        "primary_credit_before_secondary_last_execution": (
            first(primary["credits"]),
            last(secondary["executions"]),
        ),
        "primary_event_before_secondary_approval": (
            first(primary["events"]),
            first(secondary["approvals"]),
        ),
        "primary_credit_before_secondary_last_debit": (
            first(primary["credits"]),
            last(secondary["debits"]),
        ),
        "primary_event_before_secondary_last_execution": (
            first(primary["events"]),
            last(secondary["executions"]),
        ),
        "primary_first_before_secondary_first": (
            first(primary["executions"]),
            first(secondary["executions"]),
        ),
        "primary_last_before_secondary_approval": (
            last(primary["executions"]),
            first(secondary["approvals"]),
        ),
        "primary_approval_before_secondary_execution": (
            first(primary["approvals"]),
            first(secondary["executions"]),
        ),
        "primary_execution_before_secondary_proposal": (
            first(primary["executions"]),
            first(secondary["proposals"]),
        ),
    }
    if relation in values:
        left, right = values[relation]
        return left is not None and right is not None and left < right, dict(left=left, right=right)
    if relation in ("primary_sandwiches_secondary_start", "secondary_start_inside_primary_span"):
        left, middle, right = (
            first(primary["executions"]),
            first(secondary["executions"]),
            last(primary["executions"]),
        )
        return None not in (left, middle, right) and left < middle < right, dict(
            left=left, middle=middle, right=right
        )
    if relation == "secondary_start_before_primary_event_before_finish":
        left, middle, right = (
            first(secondary["executions"]),
            first(primary["events"]),
            last(secondary["executions"]),
        )
        return None not in (left, middle, right) and left < middle < right, dict(
            left=left, middle=middle, right=right
        )
    return False, dict(error="UNKNOWN_RELATION")


def component_initial(contract, component):
    """Use the composed catalogue for deterministic ID allocation."""
    merged = deepcopy(contract["initial_world"])
    merged.pop("link_contracts", None)
    source = component["initial_world"]
    merged["events"] = deepcopy(source.get("events", {}))
    merged["_event_payloads"] = deepcopy(source.get("_event_payloads", {}))
    merged["audit"] = []
    merged["unlocked"] = []
    merged["requests"] = {}
    merged["case_outcome"] = None
    merged["minute"] = 0
    return merged


def project(world, contract, component, restore_initial=None):
    """Hide the other active case while retaining shared background rows."""
    source_initial = component["initial_world"]
    initial = restore_initial or source_initial
    case_id = component["case_id"]
    out = deepcopy(world)
    out.pop("_closure_state", None)
    own_ids = {case_id}
    for table, rows in source_initial.items():
        if isinstance(rows, dict):
            own_ids.update(
                record_id
                for record_id, record in rows.items()
                if isinstance(record, dict) and record.get("case_id") == case_id
            )
    for table, rows in list(out.items()):
        if not isinstance(rows, dict) or table.startswith("_"):
            continue
        if table == "link_contracts":
            out.pop(table, None)
            continue
        if table == "requests":
            out[table] = {
                record_id: record
                for record_id, record in rows.items()
                if record.get("arguments", {}).get("case_id") == case_id
            }
            continue
        if table == "events":
            out[table] = {
                record_id: record
                for record_id, record in rows.items()
                if record.get("target_id") in own_ids or record_id in initial.get("events", {})
            }
            continue
        projected = {}
        for record_id, record in rows.items():
            if not isinstance(record, dict):
                projected[record_id] = record
            elif record_id in own_ids or record.get("case_id") == case_id:
                projected[record_id] = record
            elif record_id in initial.get(table, {}):
                projected[record_id] = deepcopy(initial[table][record_id])
            # Rows introduced by the other component or by a newer shared-base
            # catalogue are absent from this source contract and stay outside
            # its isolated acceptance world.
        out[table] = projected
    out["_event_payloads"] = {
        event_id: payload
        for event_id, payload in out.get("_event_payloads", {}).items()
        if event_id in out.get("events", {})
    }
    if world.get("_closure_state"):
        closure = project(world["_closure_state"], contract, component, initial)
        out["_closure_state"] = closure
    return out


def verify(world, contract, *, milestone_reader=milestones, relation_checker=relation_value):
    if not contract.get("link_operator"):
        from payment.runtime.evaluation.products.joint_funding.verifier import (
            verify as inherited_verify,
        )

        return inherited_verify(world, contract)
    state = world.get("_closure_state") or world
    checks = []

    def check(name, passed, description_zh, **evidence):
        checks.append(
            dict(name=name, passed=bool(passed), description_zh=description_zh, evidence=evidence)
        )

    try:
        link = contract["initial_world"]["link_contracts"][contract["link_contract_id"]]
        current_link = state["link_contracts"][link["id"]]
        check(
            "link.contract_unchanged",
            current_link == link,
            "Connection terms have not been modified, The applicable two businesses and strategies remain frozen. ",
        )
        primary = milestone_reader(world, contract, link["primary_case_id"])
        secondary = milestone_reader(world, contract, link["secondary_case_id"])
        passed, evidence = relation_checker(link["relation"], primary, secondary)
        check(
            "link." + link["operator_id"],
            passed,
            "Actual proposal, Approval, Execute, Cash flow or asynchronous events satisfy this question’s linking terms. ",
            relation=link["relation"],
            primary=primary,
            secondary=secondary,
            **evidence,
        )
        for index, component in enumerate(contract["components"], 1):
            native_contract = deepcopy(component)
            native_contract["initial_world"] = component_initial(contract, component)
            result = native_verify(
                project(world, contract, component, native_contract["initial_world"]),
                native_contract,
            )
            for row in result["checks"]:
                checks.append(
                    {
                        **row,
                        "name": f"activity{index}:" + row["name"],
                        "description_zh": component.get("title", component["family"])
                        + ": "
                        + row.get("description_zh", ""),
                    }
                )
        closure_calls = [
            row for row in world.get("audit", []) if row["tool"] == "record_case_outcome"
        ]
        outcome = world.get("case_outcome")
        check(
            "closure.single_completed",
            len(closure_calls) == 1
            and isinstance(outcome, dict)
            and outcome.get("outcome") == "COMPLETED",
            "Close exactly once after all original business and connection conditions are completed. ",
            count=len(closure_calls),
        )
        frame = True
        initial = contract["initial_world"]
        for table, rows in initial.items():
            if (
                not isinstance(rows, dict)
                or table.startswith("_")
                or table in ("requests", "events")
            ):
                continue
            for record_id, record in rows.items():
                if isinstance(record, dict) and record.get("scope") != contract["scope"]:
                    frame = frame and state.get(table, {}).get(record_id) == record
        check(
            "link.global_frame",
            frame,
            "Other customers’ accounts, Records and transactions have not been modified. ",
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        check(
            "link.well_formed",
            False,
            "Records or associations required for connection acceptance are incomplete.. ",
            error=type(exc).__name__ + ": " + str(exc),
        )
    failed = [row["name"] for row in checks if not row["passed"]]
    return dict(
        success=not failed,
        failed=failed,
        checks=checks,
        allowed_terminals=["COMPLETED"],
        verifier="diverse-link-independent-1.0",
    )
