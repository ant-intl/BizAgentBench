from copy import deepcopy
from payment.runtime.products.review.specification import SPECS as OLD_SPECS, CAPABILITIES as OLD

SPECS = deepcopy(OLD_SPECS)
SPECS["search_records"][
    "description"
] += " Coordinated service funding is recorded in programme_orders, programme_accounts, programme_routes and programme_transfers; these collections support the same scoped queries and pagination."
CAPABILITIES = {
    **OLD,
    "propose_programme_draw": dict(
        name="propose_programme_draw",
        rule_id="PROGRAMME-funding",
        description="Prepare one draw from an existing cash arrangement to a named service wallet. Read the arrangement and route terms. Requires customer approval; does not complete the underlying service.",
        parameters={
            "order_id": "string",
            "route_id": "string",
            "destination_case_id": "string",
            "amount_minor": "integer",
            "terms_version": "integer",
        },
    ),
}
