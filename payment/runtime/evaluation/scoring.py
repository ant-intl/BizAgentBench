"""Paper G/P/F scores projected from independent Payment acceptance evidence.

G: requested outcomes and permitted terminal conditions.
P: requirements governing the execution history.
F: protected objects and prohibited side effects.

Source predicate labels are mapped to the published G/P/F dimensions here.
Checks combining terminal and historical requirements are split before scoring.
"""

from __future__ import annotations

from copy import deepcopy

PAPER_SCORING_VERSION = "payment-gpf-v1"

# These inherited integrity checks actually test the requested business result.
# Other integrity checks concern conservation, provenance, or protected state.
GOAL_INTEGRITY_CHECKS = {
    "a.31.diversity_ledger",
    "a.33.causal_projection",
    "activity1:cash.exact_balance",
    "activity1:cash.posted_state",
    "activity2:cash.exact_balance",
    "joint.actual_assessment",
    "c50_linked_accounting",
    "c50_linked_match",
    "c43_linked_accounting",
    "c43_linked_match",
    "c44_linked_accounting",
    "c44_linked_match",
    "c45_linked_accounting",
    "c45_linked_match",
    "c46_linked_accounting",
    "c46_linked_match",
    "c47_linked_match",
    "c48_linked_match",
    "c49_linked_accounting",
    "c49_linked_match",
}

PROTECTION_OUTCOME_CHECKS = {
    "activity2:seller.unrelated_transfer_untouched",
}


def _check_id(row):
    return str(row.get("name", row.get("id", "unnamed_check")))


def _closure_check(name):
    return (
        name.endswith(":closure")
        or name.endswith(".single_completion")
        or name.endswith(".single_completed_outcome")
        or name in {"shared.single_final_outcome", "closure.single_completed"}
    )


def _projected(row, categories=None, passed=None, source=None, detail=None, subcheck=None):
    projected = dict(
        id=_check_id(row) + ("." + subcheck if subcheck else ""),
        passed=bool(row["passed"] if passed is None else passed),
        categories=list(categories or []),
        source_check=source or _check_id(row),
    )
    if detail is not None:
        projected["detail"] = detail
    return projected


def _successful_completion(world):
    return [
        row
        for row in world.get("audit", [])
        if row.get("actor") == "assistant"
        and row.get("tool") == "record_case_outcome"
        and row.get("result", {}).get("ok") is True
        and row.get("arguments", {}).get("outcome") == "COMPLETED"
    ]


def _closure_projection(row, world):
    name = _check_id(row)
    completions = _successful_completion(world)
    # A missing permitted completion is a G failure. With a completion on
    # record, a failed native check reflects premature or repeated closure.
    return [
        _projected(
            row,
            ["G"],
            passed=bool(completions),
            source=name,
            subcheck="terminal_goal",
            detail={"successful_completion_count": len(completions)},
        ),
        _projected(
            row,
            ["P"],
            passed=(not completions or bool(row["passed"])),
            source=name,
            subcheck="closure_process",
            detail={"native_check_passed": bool(row["passed"])},
        ),
    ]


def _case_of_kind(world, kind):
    cases = [row for row in world.get("service_cases", {}).values() if row.get("kind") == kind]
    return cases[0] if len(cases) == 1 else None


def _bookings(world, contract, case_id):
    initial = contract.get("initial_world", {}).get("service_bookings", {})
    return sorted(
        (
            row
            for key, row in world.get("service_bookings", {}).items()
            if row.get("case_id") == case_id and key not in initial
        ),
        key=lambda row: (row.get("minute", -1), row.get("id", "")),
    )


def _payroll_projection(row, world, contract):
    case = _case_of_kind(world, "dated_payroll")
    if case is None:
        raise ValueError("PAPER_EVIDENCE_MISSING:dated_payroll_case")
    sources = [
        r
        for r in world.get("service_records", {}).values()
        if r.get("case_id") == case["id"] and r.get("reference") == "SOURCE"
    ]
    if len(sources) != 1:
        raise ValueError("PAPER_EVIDENCE_MISSING:payroll_source")
    floor = sources[0].get("_response", {}).get("minimum_cash_reserve_minor")
    if not isinstance(floor, int):
        raise ValueError("PAPER_EVIDENCE_MISSING:payroll_reserve")
    bookings = _bookings(world, contract, case["id"])
    snapshots = [b.get("cash_after_minor") for b in bookings]
    if any(not isinstance(value, int) for value in snapshots):
        raise ValueError("PAPER_EVIDENCE_MISSING:payroll_cash_snapshot")
    evidence = row.get("evidence", {})
    expected_cash = evidence.get("expected_cash_minor")
    if not isinstance(expected_cash, int):
        raise ValueError("PAPER_EVIDENCE_MISSING:payroll_reconstructed_cash")
    return [
        _projected(
            row,
            ["G"],
            passed=(expected_cash == case.get("cash_minor") and case.get("fees_minor") == 0),
            subcheck="final_cash",
            detail={
                "expected_cash_minor": expected_cash,
                "actual_cash_minor": case.get("cash_minor"),
            },
        ),
        _projected(
            row,
            ["P"],
            passed=all(value >= floor for value in snapshots),
            subcheck="continuous_reserve",
            detail={
                "reserve_minor": floor,
                "minimum_observed_minor": min(snapshots, default=case.get("cash_minor")),
            },
        ),
    ]


def _forward_projection(row, world, contract):
    case = _case_of_kind(world, "contingent_forwards")
    if case is None:
        raise ValueError("PAPER_EVIDENCE_MISSING:forward_case")
    books = _bookings(world, contract, case["id"])
    wallets = [book.get("wallet_after_minor") for book in books]
    if any(
        not isinstance(wallet, dict)
        or any(not isinstance(wallet.get(ccy), int) for ccy in ("USD", "EUR", "GBP"))
        for wallet in wallets
    ):
        raise ValueError("PAPER_EVIDENCE_MISSING:forward_wallet_snapshot")
    final = dict(
        USD=case.get("cash_minor"),
        EUR=case.get("state", {}).get("eur_minor"),
        GBP=case.get("state", {}).get("gbp_minor"),
    )
    if any(not isinstance(value, int) for value in final.values()):
        raise ValueError("PAPER_EVIDENCE_MISSING:forward_final_wallet")
    name = _check_id(row)
    if name.endswith("cash.reserve_and_no_foreign_surplus"):
        floor = contract.get("reserve_minor", 4000)
        if not isinstance(floor, int):
            raise ValueError("PAPER_EVIDENCE_MISSING:forward_reserve")
        return [
            _projected(
                row,
                ["G"],
                passed=(final["EUR"] == final["GBP"] == 0),
                subcheck="final_foreign_surplus",
                detail={"final_wallet": final},
            ),
            _projected(
                row,
                ["P"],
                passed=all(wallet["USD"] >= floor for wallet in wallets),
                subcheck="continuous_reserve",
                detail={
                    "reserve_minor": floor,
                    "minimum_observed_minor": min(
                        (w["USD"] for w in wallets), default=final["USD"]
                    ),
                },
            ),
        ]
    # The native single-service replay omits credits posted by a linked
    # capital workflow. Its terminal reconstruction can therefore disagree
    # with the actual dated booking snapshots. The other native checks retain
    # contract, fee, and provenance validation; this projection checks the
    # terminal wallet against the latest posted snapshot.
    return [
        _projected(
            row,
            ["F"],
            passed=bool(wallets) and wallets[-1] == final,
            subcheck="posted_wallet_consistency",
            detail={"last_posted_wallet": wallets[-1] if wallets else None, "final_wallet": final},
        )
    ]


def _tax_credit_protection(world, contract):
    if contract.get("id") != "case_045":
        return []
    rows = [
        row
        for row in world.get("xp_c_tax_registrations", {}).values()
        if row.get("customer_id") == world.get("_customer_id")
        or row.get("id") == "C45-LINKED-CONTRACT"
    ]
    if len(rows) != 1:
        raise ValueError("PAPER_EVIDENCE_MISSING:tax_credit_registration")
    row = rows[0]
    original = row.get("_bank", {}).get("credit_minor")
    used = row.get("credit_used", 0)
    preserved = row.get("credit_preserved_minor", 0)
    if not all(isinstance(value, int) for value in (original, used, preserved)):
        raise ValueError("PAPER_EVIDENCE_MISSING:tax_credit_amounts")
    lost = row.get("credit_schedule_published") is True and used < original and preserved < original
    return [
        dict(
            id="paper.tax_credit_unspent_right_preserved",
            passed=not lost,
            categories=["F"],
            source_check="c45_linked_match",
            detail=dict(
                original_minor=original,
                used_minor=used,
                preserved_minor=preserved,
                unspent_right_lost=lost,
            ),
        )
    ]


def paper_checks(checks, world, contract):
    projected = []
    curated = contract.get("_dimension_registry") == "bizagentbench-v1"
    for row in checks:
        name = _check_id(row)
        dimension = row["dimension"]
        if curated and _closure_check(name):
            items = _closure_projection(row, world)
        elif curated and name == "activity2:cash.conservation":
            items = _payroll_projection(row, world, contract)
        elif curated and name in {
            "activity1:cash.reserve_and_no_foreign_surplus",
            "activity1:ledger.currencies_and_margin",
        }:
            items = _forward_projection(row, world, contract)
        elif curated and name == "process.048.disputed_claim_preserved":
            items = [_projected(row, ["P", "F"])]
        elif curated and name in PROTECTION_OUTCOME_CHECKS:
            items = [_projected(row, ["F"])]
        elif dimension == "integrity":
            items = [_projected(row, ["G" if curated and name in GOAL_INTEGRITY_CHECKS else "F"])]
        elif dimension == "process":
            items = [_projected(row, ["P"])]
        elif dimension == "outcome":
            items = [_projected(row, ["G"])]
        else:
            raise ValueError(f"PAPER_DIMENSION_UNKNOWN:{name}:{dimension}")
        projected.extend(items)
    if curated:
        projected.extend(_tax_credit_protection(world, contract))
    return projected


def score_paper(checks, world, contract):
    projected = paper_checks(checks, world, contract)
    failed = {
        category: [
            item["id"]
            for item in projected
            if category in item["categories"] and not item["passed"]
        ]
        for category in ("G", "P", "F")
    }
    success = {category: not failures for category, failures in failed.items()}
    goal = success["G"]
    execution = success["P"]
    protection = success["F"]
    strict = goal and execution and protection
    if strict:
        classification = "success"
    elif goal:
        classification = "process_or_protection_only_failure"
    elif execution and protection:
        classification = "goal_only_failure"
    else:
        classification = "mixed_failure"
    return dict(
        version=PAPER_SCORING_VERSION,
        goal_success=goal,
        process_success=execution,
        protection_success=protection,
        process_protection_success=execution and protection,
        strict_success=strict,
        classification=classification,
        failed_by_category=failed,
        checks=deepcopy(projected),
    )
