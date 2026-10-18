"""Operations Desk public tool definitions."""

from copy import deepcopy
from payment.runtime.core.schemas.tools import spec
from payment.runtime.products.banking.tools import SPECS as OLD_SPECS
from payment.runtime.products.payments.specification import TABLES

SPECS = deepcopy(OLD_SPECS)

SPECS["search_records"]["description"] += (
    " Additional Operations Desk resources: " + ", ".join(TABLES) + "."
)

SPECS["get_record"][
    "description"
] += " The same additional Operations Desk resources are available."

SPECS["complete_portal_verification"] = spec(
    "user",
    "Complete a real challenge using a reachable channel you control; this does not create bank approval or choose an outcome.",
    (("challenge_id", str),),
)

SPECS["respond_group_invitation"] = spec(
    "user",
    "ACCEPT or DECLINE a real pending invitation for a controlled child account. Acceptance changes membership and capacity usage.",
    (("member_id", str), ("decision", str)),
)
