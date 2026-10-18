"""Joint funding verification with explicit record projection dependencies."""

from payment.runtime.evaluation.funding.funding_accounting import verifier as prior
from payment.runtime.evaluation.funding.contract_projection.projection import (
    project as base_project,
)


def project(world, contract, component):
    return prior.project(world, contract, component, projector=base_project)


def verify(world, contract):
    if contract.get("joint_capital") and not contract.get("link_operator"):
        return prior.joint.verify(
            world, contract, projector=project, component_verifier=prior.component_verify
        )
    return prior.frozen_verify(world, contract)
