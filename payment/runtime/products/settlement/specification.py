from copy import deepcopy
from payment.runtime.products.services.specification import (
    SPECS as OLD_SPECS,
    CAPABILITIES as OLD_CAPABILITIES,
)
from payment.runtime.products.settlement.mechanisms import ENTRIES

SPECS = deepcopy(OLD_SPECS)
NEW = {m.tool_name: m.capability() for m in ENTRIES}
CAPABILITIES = {**deepcopy(OLD_CAPABILITIES), **NEW}
