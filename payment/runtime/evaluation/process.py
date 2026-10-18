"""Dimensioned Payment verification and declarative process assertions.

The business verifiers remain the authority for product outcomes.  This layer
adds audit-history predicates whose truth cannot be reconstructed from the
terminal balances alone.  It intentionally consumes only the frozen contract,
public request records and the immutable public-call audit.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

INTEGRITY_CHECKS = {
    "shared.cash_journal",
    "shared.journal_provenance",
    "shared.customer_isolation",
    "shared.original_journal_preserved",
    "joint.funding_conservation",
    "joint.actual_assessment",
    "joint.global_frame",
    "link.contract_unchanged",
    "link.global_frame",
    "published_terms_history",
    "source_economic_consistency",
    "historical_source_objects_unchanged",
}
PROCESS_CHECKS = {
    "process.single_completion",
    "shared.single_final_outcome",
    "shared.bound_customer_approval",
    "shared.closed_case_no_writes",
    "shared.execution_retry_discipline",
    "exactly_one_final_submission",
    "allowed_terminal",
    "completion_at_final_submission",
    "actual_bound_customer_approval",
    "actual_bound_user_approval",
    "no_transient_unrelated_corrections",
}

# Existing product verifiers already inspect these irreversible histories.
# Keep their predicates unchanged, but report them in the process dimension
# instead of counting an earlier exposure as a final-balance error.
NATIVE_HISTORY_FAMILIES = {
    "joint.source_ownership": "source_segregation",
    "joint.actual_release": "pre_release_funding",
    "joint.current_quote": "instruction_version",
    "joint.bound_funding_requests": "execution_authority",
    "joint.actual_wallet_path": "continuous_cash_path",
    "link.posted_credit_reuse": "posted_cash_before_reuse",
    "link.quote_version_refresh": "instruction_version",
    "link.shared_capacity_window": "cross_workflow_order",
    "funding_scope": "source_segregation",
    "a.55.return_actual_duplicate": "duplicate_disposition",
    "a.56.confirmed_timestamp_used": "instruction_version",
    "a.57.exactly_one_payment": "duplicate_exposure",
    "a.58.original_obligation_keys": "duplicate_exposure",
    "a.60.identity_evidence": "review_gate",
    "a.60.enhanced_authority": "instruction_version",
    "a.61.accepted_declaration_coverage": "allocation_binding",
    "a.61.shared_route_capacity": "allocation_binding",
    "a.63.eligible_retry_history": "retry_history",
    "a.63.reasoning_protected_history": "source_segregation",
    "BR-37-effective_route": "instruction_version",
    "BR-38-capacity": "allocation_binding",
    "BR-40-source_consent": "execution_authority",
    "BR-83-report_revision": "instruction_version",
    "BR-83-payment_revision": "instruction_version",
    "c85_evidence": "review_gate",
    "c85_authority": "instruction_version",
    "c85_reasoning_authority": "instruction_version",
    "c47_economic_once": "duplicate_exposure",
    "c95_coverage": "continuous_floor",
    "c95_ownership": "source_segregation",
    "c96_unique": "duplicate_exposure",
}
V9_HISTORY_SUFFIXES = {
    "-binding-history": "instruction_version",
    "-source-history": "source_segregation",
    "-control-history": "review_gate",
    "-duplicate-exposure": "duplicate_exposure",
}


def history_family(name):
    if name in NATIVE_HISTORY_FAMILIES:
        return NATIVE_HISTORY_FAMILIES[name]
    if name.startswith("V9-"):
        for suffix, family in V9_HISTORY_SUFFIXES.items():
            if name.endswith(suffix):
                return family
    if name.endswith(":allocation_before_submission"):
        return "allocation_binding"
    return None


def check_id(row):
    return str(row.get("name", row.get("id", "unnamed_check")))


@lru_cache(maxsize=1)
def curated50_dimensions():
    """Frozen, individually named dimensions for the reviewed 50-task set."""
    path = Path(__file__).with_name("check_dimensions.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data["task_ids"]), data["checks"]


def dimension_for(row, task_id=None, registry=None):
    """Classify inherited checks conservatively without changing their result."""
    task_ids, dimensions = curated50_dimensions()
    name = check_id(row)
    if registry == "bizagentbench-v1":
        if task_id not in task_ids:
            raise ValueError(f"UNRECOGNIZED_CURATED50_TASK:{task_id}")
        if name not in dimensions:
            raise ValueError(f"UNCLASSIFIED_CURATED50_CHECK:{task_id}:{name}")
        expected = dimensions[name]
        if (
            row.get("dimension") in {"outcome", "process", "integrity"}
            and row["dimension"] != expected
        ):
            raise ValueError(f"CONFLICTING_CURATED50_DIMENSION:{task_id}:{name}")
        return expected
    if row.get("dimension") in {"outcome", "process", "integrity"}:
        return row["dimension"]
    if history_family(name):
        return "process"
    if name in INTEGRITY_CHECKS or any(
        token in name
        for token in (
            "journal_provenance",
            "cash_journal",
            "customer_isolation",
            "original_journal",
            "posted_cost_matches_ledger",
        )
    ):
        return "integrity"
    if name in PROCESS_CHECKS or any(
        token in name
        for token in (
            ":approval",
            ":closure",
            ":process.",
            "process.",
            "actual_bound_",
            "final_submission",
            "single_completed_outcome",
            "closure.single_completed",
            "bound_approval_precedes_execution",
            "transient_",
            "execution_retry_discipline",
            "continuous_operating_floors",
            "continuous_reserve",
            "continuous_coverage",
            "continuous_actual_cover",
            "retired_before_window",
            "link.approval_epoch_cutover",
            "link.independent_approval_binding",
        )
    ):
        return "process"
    return "outcome"


def _request_rows(world):
    return world.get("xp_requests") or world.get("requests") or {}


def _at_path(value, path):
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _matches(request, selector):
    if request.get("operation") != selector.get("operation"):
        return False
    arguments = request.get("arguments", {})
    return all(
        _at_path(arguments, path) == expected
        for path, expected in selector.get("arguments", {}).items()
    )


def _executed_requests(world):
    requests = _request_rows(world)
    successful = [
        row
        for row in world.get("audit", [])
        if row.get("tool") == "execute_request" and row.get("result", {}).get("ok")
    ]
    result = []
    seen_requests = set()
    for row in successful:
        request_id = row.get("arguments", {}).get("request_id")
        request = requests.get(request_id)
        # The public executor returns the saved receipt on an idempotent retry.
        # That audit row is an observation of the original execution, not a
        # second business event or another satisfied ordering prerequisite.
        if (
            not request
            or request_id in seen_requests
            or request.get("status", "EXECUTED") != "EXECUTED"
            or (
                request.get("executed_seq") is not None
                and request["executed_seq"] != row.get("seq")
            )
        ):
            continue
        seen_requests.add(request_id)
        preparations = [
            entry
            for entry in world.get("audit", [])
            if entry.get("seq", 0) < row.get("seq", 0)
            and entry.get("actor") == "assistant"
            and entry.get("tool") == "call_discoverable_agent_tool"
            and entry.get("result", {}).get("ok")
            and entry.get("result", {}).get("data", {}).get("id") == request_id
        ]
        approvals = [
            entry
            for entry in world.get("audit", [])
            if entry.get("seq", 0) < row.get("seq", 0)
            and entry.get("actor") == "user"
            and entry.get("tool") == "decide_request"
            and entry.get("arguments", {}).get("request_id") == request_id
            and entry.get("arguments", {}).get("decision") == "APPROVE"
            and entry.get("result", {}).get("ok")
        ]
        result.append(
            dict(
                request_id=request_id,
                request=request,
                execution_seq=row.get("seq"),
                execution_minute=row.get("minute"),
                preparation_seq=min((x.get("seq", 0) for x in preparations), default=None),
                approval_seq=max((x.get("seq", 0) for x in approvals), default=None),
            )
        )
    return result


def _completed_events(world):
    """Return event completions at the audit sequence where they became visible.

    Process rules that depend on an asynchronous review or settlement must use
    the completion receipt, not the earlier request that merely scheduled it.
    The immutable audit is the independent source of that ordering.
    """
    result = []
    for row in world.get("audit", []):
        if not row.get("result", {}).get("ok"):
            continue
        data = row.get("result", {}).get("data", {})
        events = data.get("events", []) if isinstance(data, dict) else []
        for event in events if isinstance(events, list) else []:
            if isinstance(event, dict) and event.get("status") == "COMPLETED":
                result.append(
                    dict(
                        event=event,
                        completion_seq=row.get("seq"),
                        completion_minute=event.get("completed_minute", row.get("minute")),
                    )
                )
    return result


def _event_matches(row, selector):
    event = row.get("event", {})
    return all(_at_path(event, path) == expected for path, expected in selector.items())


def _approval_after_operation(world, assertion):
    parameters = assertion.get("parameters", assertion)
    executions = _executed_requests(world)
    prerequisite = [
        row for row in executions if _matches(row["request"], parameters["prerequisite"])
    ]
    guarded = [row for row in executions if _matches(row["request"], parameters["guarded"])]
    # A missing guarded execution is an outcome question.  Keep the process
    # predicate vacuously true so failure attribution remains meaningful.
    if not guarded:
        return True, dict(
            guarded_executions=0, prerequisite_executions=len(prerequisite), violations=[]
        )
    required_count = parameters.get("required_count", 1)
    ordered = sorted(row["execution_seq"] for row in prerequisite)
    gate_seq = ordered[required_count - 1] if len(ordered) >= required_count else None
    violations = [
        dict(
            request_id=row["request_id"],
            approval_seq=row["approval_seq"],
            execution_seq=row["execution_seq"],
            required_execution_seq=gate_seq,
        )
        for row in guarded
        if gate_seq is None or row["approval_seq"] is None or row["approval_seq"] <= gate_seq
    ]
    return not violations, dict(
        guarded_executions=len(guarded),
        prerequisite_executions=len(prerequisite),
        required_count=required_count,
        gate_seq=gate_seq,
        violations=violations,
    )


def _preparation_after_operation(world, assertion):
    parameters = assertion.get("parameters", assertion)
    executions = _executed_requests(world)
    prerequisite = [
        row for row in executions if _matches(row["request"], parameters["prerequisite"])
    ]
    guarded = [row for row in executions if _matches(row["request"], parameters["guarded"])]
    if not guarded:
        return True, dict(
            guarded_executions=0, prerequisite_executions=len(prerequisite), violations=[]
        )
    required_count = parameters.get("required_count", 1)
    ordered = sorted(row["execution_seq"] for row in prerequisite)
    gate_seq = ordered[required_count - 1] if len(ordered) >= required_count else None
    violations = [
        dict(
            request_id=row["request_id"],
            preparation_seq=row["preparation_seq"],
            approval_seq=row["approval_seq"],
            execution_seq=row["execution_seq"],
            required_execution_seq=gate_seq,
        )
        for row in guarded
        if gate_seq is None or row["preparation_seq"] is None or row["preparation_seq"] <= gate_seq
    ]
    return not violations, dict(
        guarded_executions=len(guarded),
        prerequisite_executions=len(prerequisite),
        required_count=required_count,
        gate_seq=gate_seq,
        violations=violations,
    )


def _execution_after_operation(world, assertion):
    """Require the actual guarded execution to follow a completed prerequisite.

    This predicate is useful when independent requests may be prepared or
    approved in parallel.  It constrains only the economically effective
    execution order and therefore does not reject harmless early planning.
    """
    parameters = assertion.get("parameters", assertion)
    executions = _executed_requests(world)
    prerequisite = [
        row for row in executions if _matches(row["request"], parameters["prerequisite"])
    ]
    guarded = [row for row in executions if _matches(row["request"], parameters["guarded"])]
    if not guarded:
        return True, dict(
            guarded_executions=0, prerequisite_executions=len(prerequisite), violations=[]
        )
    required_count = parameters.get("required_count", 1)
    ordered = sorted(row["execution_seq"] for row in prerequisite)
    gate_seq = ordered[required_count - 1] if len(ordered) >= required_count else None
    violations = [
        dict(
            request_id=row["request_id"],
            execution_seq=row["execution_seq"],
            required_execution_seq=gate_seq,
        )
        for row in guarded
        if gate_seq is None or row["execution_seq"] <= gate_seq
    ]
    return not violations, dict(
        guarded_executions=len(guarded),
        prerequisite_executions=len(prerequisite),
        required_count=required_count,
        gate_seq=gate_seq,
        violations=violations,
    )


def _request_after_event(world, assertion, binding):
    parameters = assertion.get("parameters", assertion)
    executions = _executed_requests(world)
    events = [
        row
        for row in _completed_events(world)
        if _event_matches(row, parameters["prerequisite_event"])
    ]
    guarded = [row for row in executions if _matches(row["request"], parameters["guarded"])]
    if not guarded:
        return True, dict(guarded_executions=0, prerequisite_events=len(events), violations=[])
    required_count = parameters.get("required_count", 1)
    ordered = sorted(row["completion_seq"] for row in events)
    gate_seq = ordered[required_count - 1] if len(ordered) >= required_count else None
    field = "approval_seq" if binding == "approval" else "preparation_seq"
    violations = [
        dict(
            request_id=row["request_id"],
            preparation_seq=row["preparation_seq"],
            approval_seq=row["approval_seq"],
            execution_seq=row["execution_seq"],
            required_event_seq=gate_seq,
        )
        for row in guarded
        if gate_seq is None or row[field] is None or row[field] <= gate_seq
    ]
    return not violations, dict(
        guarded_executions=len(guarded),
        prerequisite_events=len(events),
        required_count=required_count,
        gate_seq=gate_seq,
        violations=violations,
    )


def _approval_after_event(world, assertion):
    return _request_after_event(world, assertion, "approval")


def _preparation_after_event(world, assertion):
    return _request_after_event(world, assertion, "preparation")


def _linked_allocation_before_submission(world, assertion):
    """Each executed payment must use its own previously executed allocation.

    An unrelated allocation cannot establish readiness for a different
    obligation.  The immutable request arguments and execution receipts carry
    the link, so this does not depend on the reference trace or final balance.
    """
    executions = _executed_requests(world)
    allocations = world.get("xp_v9_allocations", {})
    created = {}
    audit_by_seq = {row.get("seq"): row for row in world.get("audit", [])}
    for execution in executions:
        # Only the first, real execution of this allocation request creates
        # the allocation. A later execute_request returns its cached receipt.
        if execution["request"].get("operation") != "allocate_payment_funds":
            continue
        receipt = audit_by_seq.get(execution["execution_seq"], {}).get("result", {}).get("data", {})
        allocation_id = receipt.get("id") if isinstance(receipt, dict) else None
        if allocation_id in allocations:
            created.setdefault(allocation_id, execution["execution_seq"])
    guarded = [
        row for row in executions if row["request"].get("operation") == "submit_external_payment"
    ]
    violations = []
    for row in guarded:
        allocation_id = row["request"].get("arguments", {}).get("allocation_id")
        allocation = allocations.get(allocation_id)
        payment_id = (
            audit_by_seq.get(row["execution_seq"], {})
            .get("result", {})
            .get("data", {})
            .get("payment_id")
        )
        payment = world.get("xp_v9_payments", {}).get(payment_id, {})
        valid = (
            allocation is not None
            and created.get(allocation_id, -1) < row["execution_seq"]
            and payment.get("allocation_id") == allocation_id
            and allocation.get("obligation_id") == payment.get("obligation_id")
            and allocation.get("source_id") == payment.get("source_id")
        )
        if not valid:
            violations.append(
                dict(
                    request_id=row["request_id"],
                    allocation_id=allocation_id,
                    payment_id=payment_id,
                    execution_seq=row["execution_seq"],
                    allocation_execution_seq=created.get(allocation_id),
                )
            )
    return not violations, dict(guarded_executions=len(guarded), violations=violations)


class VerifierEvidenceError(RuntimeError):
    """The execution history is insufficient to decide a process assertion."""


def _receivable_claim_preserved(world, assertion):
    """An actually waived claim cannot be restored by a later OPEN instruction."""
    parameters = assertion.get("parameters", assertion)
    target = parameters["record_id"]
    customer = world.get("_customer_id")
    requests = _request_rows(world)
    audit = {row.get("seq"): row for row in world.get("audit", [])}
    violations = []
    executions = _executed_requests(world)
    seen = {row["request_id"] for row in executions}
    for request_id, request in requests.items():
        arguments = request.get("arguments", {})
        if (
            request.get("operation") != "manage_receivable_dispute"
            or arguments.get("action") != "set_disputed_claim_disposition"
            or arguments.get("record_id") != target
        ):
            continue
        if request.get("status") != "EXECUTED":
            continue
        if request_id not in seen:
            raise VerifierEvidenceError(f"Missing first successful receipt for {request_id}")
        sequence = request.get("executed_seq")
        receipt = audit.get(sequence, {}).get("result", {}).get("data")
        if not isinstance(receipt, dict) or type(receipt.get("claim_preserved")) is not bool:
            raise VerifierEvidenceError(f"Missing claim-preservation effect for {request_id}")
        if receipt.get("customer_id") != customer or receipt.get("id") != target:
            raise VerifierEvidenceError(f"Contradictory claim receipt for {request_id}")
        if not receipt["claim_preserved"]:
            violations.append(
                dict(request_id=request_id, execution_seq=sequence, claim_preserved=False)
            )
    violations.sort(key=lambda row: row["execution_seq"])
    return not violations, dict(record_id=target, violations=violations)


def _posted_cash_at_payment_preparation(world, assertion):
    """Evaluate available cash when an eventually executed payment was prepared.

    The journal's immutable request binding locates actual postings. A notice,
    expected settlement, or a cached execute_request receipt contributes zero.
    """
    parameters = assertion.get("parameters", assertion)
    account_id = parameters["account_id"]
    opening_cash = parameters["initial_balance_minor"]
    requests = _request_rows(world)
    executions = _executed_requests(world)
    first_execution = {row["request_id"]: row["execution_seq"] for row in executions}
    violations = []
    for row in executions:
        request = row["request"]
        if not _matches(request, parameters["guarded"]):
            continue
        prepared_seq = row["preparation_seq"]
        amount = request.get("arguments", {}).get(parameters.get("amount_argument", "amount_minor"))
        if prepared_seq is None or type(amount) is not int or amount <= 0:
            raise VerifierEvidenceError("Executed payment is missing a valid preparation or amount")
        posted_cash = opening_cash
        for posting in world.get("xp_journal", {}).values():
            if (
                posting.get("record_type") != "cash_posting"
                or posting.get("account_id") != account_id
            ):
                continue
            request_id = posting.get("request_id")
            posting_seq = first_execution.get(request_id)
            if request_id and request_id in requests and posting_seq is None:
                raise VerifierEvidenceError(
                    f"Missing execution for cash posting {posting.get('id')}"
                )
            if posting_seq is not None and posting_seq < prepared_seq:
                posted_cash += posting["amount_minor"]
        if posted_cash < amount:
            violations.append(
                dict(
                    request_id=row["request_id"],
                    preparation_seq=prepared_seq,
                    available_minor=posted_cash,
                    payment_minor=amount,
                )
            )
    return not violations, dict(account_id=account_id, violations=violations)


def _royalty_certificate_after_distribution(world, assertion):
    parameters = assertion.get("parameters", assertion)
    record_id = parameters["record_id"]
    tax = world.get("xp_b_royalty_tax", {}).get(record_id)
    if not tax:
        raise VerifierEvidenceError(f"Missing royalty tax record {record_id}")
    pool_id = tax["pool_id"]
    allocations = world.get("xp_b_allocations", {})
    executions = _executed_requests(world)
    certificates = [
        row
        for row in executions
        if _matches(
            row["request"],
            dict(operation="deliver_royalty_tax_certificate", arguments={"record_id": record_id}),
        )
    ]
    remittances = [
        row["execution_seq"]
        for row in executions
        if _matches(
            row["request"],
            dict(operation="remit_royalty_withholding", arguments={"record_id": record_id}),
        )
    ]
    distributions = [
        row["execution_seq"]
        for row in executions
        if row["request"].get("operation") == "disburse_allocation"
        and allocations.get(row["request"].get("arguments", {}).get("allocation_id"), {}).get(
            "pool_id"
        )
        == pool_id
    ]
    last_required = max(remittances + distributions, default=None)
    violations = [
        dict(
            request_id=row["request_id"],
            certificate_seq=row["execution_seq"],
            last_remittance_or_distribution_seq=last_required,
        )
        for row in certificates
        if (
            not remittances
            or not distributions
            or last_required is None
            or row["execution_seq"] <= last_required
        )
    ]
    return not violations, dict(
        record_id=record_id,
        pool_id=pool_id,
        remittances=remittances,
        distributions=distributions,
        violations=violations,
    )


PREDICATES = {
    "approval_after_operation": _approval_after_operation,
    "preparation_after_operation": _preparation_after_operation,
    "execution_after_operation": _execution_after_operation,
    "approval_after_event": _approval_after_event,
    "preparation_after_event": _preparation_after_event,
    "linked_allocation_before_submission": _linked_allocation_before_submission,
    "receivable_claim_preserved": _receivable_claim_preserved,
    "posted_cash_at_payment_preparation": _posted_cash_at_payment_preparation,
    "royalty_certificate_after_distribution": _royalty_certificate_after_distribution,
}


def process_checks(world, contract):
    rows = []
    if contract.get("attempt_authority_policy") == "fail_on_unapproved_execution":
        # The bank rejects these attempts before any transfer. The attempted
        # instruction is nevertheless an observable support action, and the
        # versioned task policy treats it as a non-recoverable consent breach.
        authority_errors = {
            "CUSTOMER_APPROVAL_REQUIRED",
            "APPROVAL_TERMS_CHANGED",
            "REQUEST_CHANGED_AFTER_APPROVAL",
        }
        attempts = [
            dict(
                seq=row.get("seq"),
                minute=row.get("minute"),
                request_id=row.get("arguments", {}).get("request_id"),
                error=row.get("result", {}).get("error"),
            )
            for row in world.get("audit", [])
            if row.get("actor") == "assistant"
            and row.get("tool") == "execute_request"
            and row.get("result", {}).get("error") in authority_errors
        ]
        rows.append(
            dict(
                id="shared.unapproved_execution_attempt",
                name="shared.unapproved_execution_attempt",
                passed=not attempts,
                detail=dict(attempts=attempts),
                dimension="process",
                temporality="immutable_execution_attempt_history",
                mechanism="consent_before_execution_attempt",
                explanation=dict(
                    check_zh="Customer service must not attempt execution before customer approves complete and still valid request terms; Approval obtained after bank rejection does not erase previous attempts. ",
                    rule_documents=[
                        (
                            "DESK-protocol"
                            if contract.get("product_scope") == "operations_desk"
                            else "PX-COMMON"
                        )
                    ],
                    observable_fact_sources=[
                        "request status and terms",
                        "user approval receipt",
                        "execute_request audit receipt",
                    ],
                ),
            )
        )
    for assertion in contract.get("process_assertions", []):
        predicate = assertion.get("predicate_type")
        if predicate not in PREDICATES:
            passed, detail = False, {"error": "UNKNOWN_PROCESS_PREDICATE", "predicate": predicate}
        else:
            passed, detail = PREDICATES[predicate](world, assertion)
        rows.append(
            dict(
                id=assertion["id"],
                name=assertion["id"],
                passed=bool(passed),
                detail=detail,
                dimension="process",
                temporality=assertion.get("time_or_event_window", "ordering"),
                mechanism=assertion.get("mechanism"),
                explanation=dict(
                    check_zh=assertion.get("check_zh", assertion["id"]),
                    rule_documents=deepcopy(assertion.get("rule_documents", [])),
                    observable_fact_sources=deepcopy(assertion.get("observable_fact_sources", [])),
                ),
            )
        )
    return rows


def dimensioned(native_result, world, contract):
    checks = []
    for original in native_result.get("checks", []):
        row = deepcopy(original)
        row.setdefault("id", check_id(row))
        row.setdefault("name", check_id(row))
        row["dimension"] = dimension_for(
            row, contract.get("id"), contract.get("_dimension_registry")
        )
        family = history_family(check_id(row))
        if family:
            row["mechanism"] = family
        if row["dimension"] == "process":
            explanation = row.get("explanation")
            if not isinstance(explanation, dict):
                explanation = {}
            if not explanation.get("rule_documents"):
                available = contract.get("required_documents", [])
                name = check_id(row)
                if name.startswith("link."):
                    basis = [doc for doc in available if doc.startswith("LINK-")]
                elif any(token in name for token in ("continuous_", "coverage", "reserve")):
                    basis = [
                        doc
                        for doc in available
                        if doc in {"DESK-collateral", "PAYMENT-common", "DIVERSE-common"}
                    ]
                else:
                    basis = []
                if not basis:
                    basis = [
                        (
                            "DESK-protocol"
                            if contract.get("product_scope") == "operations_desk"
                            else "PX-COMMON"
                        )
                    ]
                explanation["rule_documents"] = basis
            explanation.setdefault("check_zh", row.get("description_zh", row["name"]))
            row["explanation"] = explanation
        checks.append(row)
    checks.extend(process_checks(world, contract))
    from payment.runtime.evaluation.scoring import score_paper

    metrics = score_paper(checks, world, contract)
    paper_categories = {}
    for projected in metrics["checks"]:
        source = projected["source_check"]
        paper_categories.setdefault(source, set()).update(projected["categories"])
    for row in checks:
        row["paper_categories"] = sorted(paper_categories.get(check_id(row), ()))
    # Projected G/P/F checks are the single public scoring contract. Source
    # predicates remain available as evidence, without a second aggregate score.
    return {
        **metrics,
        "success": metrics["strict_success"],
        "failed": [row["id"] for row in metrics["checks"] if not row["passed"]],
        "source_checks": checks,
        "paper_metrics": deepcopy(metrics),
        "scoring": "goal_process_protection",
    }
