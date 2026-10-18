"""Run independent business checks and their G/P/F projection."""

from importlib import import_module

PRODUCT_VERIFIERS = {
    "receivables": "receivables.operations",
    "accounts": "accounts.operations",
    "commerce": "commerce.operations",
}


def verify(world, contract):
    verifier_family = contract.get("verifier_family", contract.get("engine_profile"))
    if verifier_family == "formal":
        from payment.runtime.evaluation.operations import verify as original

        native = original(world, contract)
    else:
        from payment.runtime.evaluation.psp.shared import verify as shared

        product = import_module(".psp." + PRODUCT_VERIFIERS[contract["business_domain"]], __package__)
        native = shared(world, contract, product.verify)
    from payment.runtime.evaluation.process import dimensioned

    return dimensioned(native, world, contract)
