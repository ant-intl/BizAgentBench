from copy import deepcopy
from payment.runtime.products.lifecycle.specification import SPECS as OLD, CAPABILITIES as OLD_CAPS

SPECS = deepcopy(OLD)
NAME = "propose_joint_capital_delivery"
CAPABILITIES = {
    **deepcopy(OLD_CAPS),
    NAME: dict(
        name=NAME,
        description="Prepare delivery of registered service capital from customer-owned settlement allocations. Read the cash allocation agreement and the current route terms. Funding does not pay a bill.",
        parameters={
            "agreement_id": "string",
            "case_id": "string",
            "route_id": "string",
            "allocations": "object",
            "terms_version": "integer",
        },
    ),
}
