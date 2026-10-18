"""Apply additional receipt checks to the supported receivables tasks."""

from payment.runtime.evaluation.psp.receivables.custody_and_credit import verify as verify_settlement


def verify(world, contract, previous):
    if contract.get("reasoning_revision") and contract["number"] in (31, 33):
        return verify_settlement(world, contract, previous)
    return previous(world, contract)


def wrap(previous):
    return lambda world, contract: verify(world, contract, previous)
