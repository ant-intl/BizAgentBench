"""Independent link milestones, with explicit public scope and execution binding.

No business transition functions or reference actions are used to score a run.
The native financial checks and side-effect/closure checks remain unchanged.
"""

from payment.runtime.evaluation.products.dependencies import verifier as legacy
from payment.runtime.evaluation.funding.contract_projection.verifier import (
    verify as inherited_verify,
)


def cash_delta(record, currency):
    if not isinstance(record, dict):
        return 0
    if type(record.get("cash_delta_minor")) is int:
        return record["cash_delta_minor"]
    if currency == "USD" and type(record.get("usd_delta_minor")) is int:
        return record["usd_delta_minor"]
    wallet = record.get("wallet_delta_minor")
    delta = wallet.get(currency, 0) if isinstance(wallet, dict) else 0
    return delta if type(delta) is int else 0


def milestones(world, contract, case_id):
    state = world.get("_closure_state") or world
    initial = contract["initial_world"]
    link = initial["link_contracts"][contract["link_contract_id"]]
    terms = link["milestone_scope"]
    currency = terms["cash_currency_by_case"][case_id]
    event_ids = set(terms["qualifying_event_ids_by_case"][case_id])
    result = {
        key: [] for key in ("proposals", "approvals", "executions", "credits", "debits", "events")
    }
    rows = [r for r in world.get("audit", []) if r.get("result", {}).get("ok")]
    executed = {}
    for row in rows:
        if (
            row["tool"] == "execute_request"
            and row["result"].get("data", {}).get("case_id") == case_id
        ):
            request_id = row["arguments"].get("request_id")
            request = state.get("requests", {}).get(request_id, {})
            if request_id in executed or (
                request.get("executed_seq") is not None and request["executed_seq"] != row["seq"]
            ):
                continue
            executed[request_id] = row["seq"]
            result["executions"].append(row["seq"])
            delta = cash_delta(row["result"]["data"], currency)
            if delta:
                result["credits" if delta > 0 else "debits"].append(row["seq"])

    # Bind to the actual request used by an execution. Abandoned proposals and
    # approvals must not poison an otherwise valid replacement request.
    for request_id, execution_seq in executed.items():
        proposals = [
            r["seq"]
            for r in rows
            if r["seq"] < execution_seq
            and r["tool"] == "call_discoverable_agent_tool"
            and r["result"].get("data", {}).get("id") == request_id
            and r["result"].get("data", {}).get("arguments", {}).get("case_id") == case_id
        ]
        approvals = [
            r["seq"]
            for r in rows
            if r["seq"] < execution_seq
            and r["tool"] == "decide_request"
            and r["arguments"].get("request_id") == request_id
            and r["arguments"].get("decision") == "APPROVE"
        ]
        if proposals:
            result["proposals"].append(min(proposals))
        if approvals:
            result["approvals"].append(max(approvals))

    # Match actual completed event IDs in a wait receipt to new native journals.
    # Minute equality alone would reuse a posting on unrelated/repeated waits.
    seen = set()
    initial_bookings = initial.get("service_bookings", {})
    for row in rows:
        if row["tool"] != "wait_for_update":
            continue
        completed = {
            e["id"]
            for e in row["result"].get("data", {}).get("events", [])
            if isinstance(e, dict) and e.get("status") == "COMPLETED"
        }
        for booking_id, booking in state.get("service_bookings", {}).items():
            if (
                booking_id in initial_bookings
                or booking_id in seen
                or booking.get("case_id") != case_id
                or booking.get("kind") != "bank_event"
                or booking.get("event_id") not in completed
                or booking.get("minute") != row["minute"]
            ):
                continue
            seen.add(booking_id)
            if booking["event_id"] in event_ids:
                result["events"].append(row["seq"])
            # Cash gates accept any posted credit of the explicitly named case
            # and currency. The event gate has a separate, narrower ID selector.
            delta = cash_delta(booking, currency)
            if delta:
                result["credits" if delta > 0 else "debits"].append(row["seq"])
    return {key: sorted(set(values)) for key, values in result.items()}


relation_value = legacy.relation_value


def _linked_verify(world, contract):
    return legacy.verify(
        world, contract, milestone_reader=milestones, relation_checker=relation_value
    )


def verify(world, contract):
    if not contract.get("link_operator"):
        result = inherited_verify(world, contract)
        if contract.get("id") == "case_003":
            # Native component projection does not include the joint reserve
            # table, so its review-fee check cannot see the release charge.
            # Assess the complete customer expenditure in the joint world.
            state = world.get("_closure_state") or world
            initial = contract["initial_world"]
            application = initial["onboarding_applications"]["JOINT01_DU-APPLICATION"]
            billing = application["billing_account_id"]
            review = sum(
                application["submission_fee_minor"]
                for request in state["requests"].values()
                if request.get("status") == "EXECUTED"
                and request.get("operation") == "propose_onboarding_submission"
                and state["evidence_bundles"][request["arguments"]["bundle_id"]]["application_id"]
                == "JOINT01_DU-APPLICATION"
            )
            releases = [
                row
                for row in state.get("reserve_disbursements", {}).values()
                if row.get("scope") == contract["scope"]
                and row.get("destination_account_id") == billing
            ]
            release_fee = sum(row.get("fee_minor", 0) for row in releases)
            permitted = 5000
            choice = contract.get("customer_fee_deadline_choice", {})
            if choice:
                approved = {
                    request["arguments"].get("route_id")
                    for request in state["requests"].values()
                    if request.get("operation") == "propose_reserve_disbursement"
                    and request.get("status") == "EXECUTED"
                    and request.get("approved_by")
                    and request.get("approval_seq", 10**9) < request.get("executed_seq", -1)
                }
                if choice.get("fee_route_id") in approved:
                    permitted = choice["accepted_total_fee_cap_minor"]
            row = dict(
                name="onboarding:total_customer_fee_budget",
                passed=review + release_fee <= permitted,
                description_zh="The institution review fee and the reserve release fee actually paid to obtain funds for it must not in total exceed the customer’s authorized cap. ",
                detail=dict(
                    review_fee_minor=review,
                    reserve_fee_minor=release_fee,
                    authorized_cap_minor=permitted,
                ),
            )
            result["checks"].append(row)
            result["failed"] = [r["name"] for r in result["checks"] if not r["passed"]]
            result["success"] = not result["failed"]
        if (
            contract.get("id") == "case_002"
            and contract.get("score_patch_revision") == "scorer-20260922"
        ):
            # The generic exit component only checks terminal service/account
            # states. This customer's disclosed deadline and final destination
            # are separate obligations of the combined task.
            state = world.get("_closure_state") or world
            initial = contract["initial_world"]
            requests = [r for r in state["requests"].values() if r["status"] == "EXECUTED"]
            close = [
                r
                for r in requests
                if r["operation"] == "propose_desk_account_close"
                and r["arguments"].get("account_id") == "JOINT05_IN-OLD"
            ]
            sweeps = [
                r["arguments"]
                for r in requests
                if r["operation"] == "propose_account_sweep"
                and r["arguments"].get("source_account_id") == "JOINT05_IN-OLD"
            ]
            second_bills = sum(
                b["amount_minor"]
                for b in state["billing_instances"].values()
                if b["id"] not in initial["billing_instances"]
                and b["service_id"] == "JOINT05_IN-RETAIN-2"
                and b["account_id"] == "JOINT05_IN-ALT"
                and b["status"] == "PAID"
            )
            needed = max(
                0, second_bills - initial["desk_accounts"]["JOINT05_IN-ALT"]["wallet"]["USD"]
            )
            second_transfer = sum(
                r["amount_minor"] for r in sweeps if r["destination_account_id"] == "JOINT05_IN-ALT"
            )
            result["checks"].extend(
                [
                    dict(
                        name="exit.customer_deadline",
                        passed=len(close) == 1 and close[0].get("executed_minute", 10**9) <= 60,
                        description_zh="Old account actually closed within one hour requested by customer. ",
                        detail=[r.get("executed_minute") for r in close],
                    ),
                    dict(
                        name="exit.surplus_destination",
                        passed=second_transfer <= needed
                        and all(
                            r["destination_account_id"] in ("JOINT05_IN-KEEP", "JOINT05_IN-ALT")
                            for r in sweeps
                        ),
                        description_zh="Payment from old account to second site covers only actual bills, The remainder goes to the main operating account. ",
                        detail=dict(
                            second_site_transfer_minor=second_transfer,
                            second_site_required_minor=needed,
                            destinations=[r["destination_account_id"] for r in sweeps],
                        ),
                    ),
                ]
            )
            result["failed"] = [r["name"] for r in result["checks"] if not r["passed"]]
            result["success"] = not result["failed"]
        return result
    result = _linked_verify(world, contract)
    result["verifier"] = "payment-link-contracts-16.1.2"
    return result
