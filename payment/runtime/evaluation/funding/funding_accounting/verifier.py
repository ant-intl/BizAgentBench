"""Preserve joint goals and distinguish physical cash from virtual capital."""

from payment.runtime.evaluation.products.dependencies.verifier import verify as frozen_verify
from payment.runtime.evaluation.products.joint_funding import verifier as joint
from payment.runtime.evaluation.funding.funding_accounting.credit_verifier import (
    verify as credit_verify,
)
from payment.runtime.evaluation.funding.funding_accounting.payment_verifier import (
    verify as payment_verify,
)


def project(world, contract, component, *, projector=joint.project):
    projected, native = projector(world, contract, component)
    if component.get("kind") == "documentary_imports" or component.get("payment_service"):
        state = world.get("_closure_state") or world
        agreement = contract["initial_world"]["joint_agreements"][contract["agreement_id"]]
        cid = component["case_id"]
        initial_undelivered = (
            agreement["capital_by_case"][cid] - agreement["initial_operating_by_case"][cid]
        )
        sequences = joint.audit_sequences(world, state)
        offsets = {}
        for bid, b in state["service_bookings"].items():
            if b.get("case_id") != cid or bid in contract["initial_world"]["service_bookings"]:
                continue
            if bid not in sequences:
                continue
            received = sum(
                f["amount_minor"]
                for f in state["joint_fundings"].values()
                if f["case_id"] == cid and sequences.get(f["id"], 10**9) < sequences[bid]
            )
            offsets[bid] = initial_undelivered - received
        native["_undelivered_capital_before_booking"] = offsets
    return projected, native


def component_verify(world, contract):
    if contract.get("kind") == "documentary_imports":
        return credit_verify(world, contract)
    if contract.get("payment_service"):
        return payment_verify(world, contract)
    if contract.get("kind") == "continuous_guarantees" and contract["id"] in (
        "trade_guarantee",
        "trade_guarantee__baseline",
    ):
        from payment.runtime.evaluation.guarantees.guarantee_verifier import (
            verify as guarantee_verify,
        )

        return guarantee_verify(world, contract)
    return joint.original_component_verify(world, contract)


def joint_verify(world, contract):
    return joint.verify(world, contract, projector=project, component_verifier=component_verify)


def verify(world, contract):
    if contract.get("joint_capital") and not contract.get("link_operator"):
        return joint_verify(world, contract)
    return frozen_verify(world, contract)
