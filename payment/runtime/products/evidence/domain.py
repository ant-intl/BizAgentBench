"""Public policy and tool schema definitions."""

from copy import deepcopy
from payment.runtime.products.payments.tools import SPECS as OLD_SPECS
from payment.runtime.products.banking.domain import POLICY as COMMON_POLICY

SPECS = deepcopy(OLD_SPECS)

SPECS["search_records"][
    "description"
] += " Business source documents are available as business_documents, filterable by kind and reference."

POLICY = (
    COMMON_POLICY
    + " Product terms state their applicable scope. Customer statements describe their goals and beliefs; use actual records for bank statuses. Tool success does not certify that an action satisfies every customer objective. Both roles share fixed scheduler and tool-call budgets; each call, including a rejected one, consumes a call."
)
