from copy import deepcopy
from payment.runtime.products.portfolio.specification import (
    CAPABILITIES as DIVERSE,
    SPECS as PUBLIC,
)
from payment.runtime.products.funding.specification import CAPABILITIES as PROGRAMME

SPECS = deepcopy(PUBLIC)
SPECS["search_records"][
    "description"
] += " Existing programme_orders, programme_accounts, programme_routes and programme_transfers remain queryable by scope."
from payment.runtime.products.services.mechanisms import ENTRIES

NEW = {m.tool_name: m.capability() for m in ENTRIES}
CAPABILITIES = {**DIVERSE, **PROGRAMME, **NEW}
