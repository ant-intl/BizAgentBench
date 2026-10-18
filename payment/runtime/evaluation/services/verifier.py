"""Independent business predicates with two documented legal-path repairs."""

from copy import deepcopy
from importlib import import_module
from payment.runtime.evaluation.products.services.verifier import verify as previous
from payment.runtime.products.settlement.mechanisms import BY_KIND
from payment.runtime.evaluation.services.guardrails import checks
from payment.runtime.evaluation.services.custody_coupon_verifier import verify as coupon_verify


def verify(world, c):
    if c.get("kind") not in BY_KIND:
        return previous(world, c)
    fn = (
        coupon_verify
        if c["kind"] == "custody_coupon_entitlements"
        else import_module(
            "payment.runtime.evaluation.products.settlement."
            + BY_KIND[c["kind"]].implementation_prefix
            + "_verifier"
        ).verify
    )
    result = deepcopy(fn(world, c))
    result["checks"].extend(checks(world, c))
    result["failed"] = [x["name"] for x in result["checks"] if not x["passed"]]
    result["success"] = not result["failed"]
    return result
