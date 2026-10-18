"""Public local operations. No task-specific capability visibility or gold IDs."""

from copy import deepcopy
from payment.runtime.products.payments.specification import CAPABILITIES as INHERITED
from payment.runtime.products.evidence.domain import SPECS as OLD_SPECS

SPECS = deepcopy(OLD_SPECS)
SPECS["search_records"][
    "description"
] += " Trade Services 7.0 also exposes service_cases, service_records, service_documents, service_bookings and service_reviews. Filter by name, kind, case_id, reference or status; read detail after search."
SPECS["get_record"][
    "description"
] += " Trade Services 7.0 resources use the same identifiers and access scope."
SPECS["wait_for_update"][
    "description"
] = "Advance to the next real bank or portal event only when support explicitly asks you to wait for that update. Waiting while support investigates means an ordinary text reply, not this tool."
NAMES = {
    "escrow": ("escrow_instruction", "Escrow acceptance, retention and distribution"),
    "factoring": ("factoring_instruction", "Invoice assignment, advance and repayment"),
    "credit_letter": (
        "documentary_credit_instruction",
        "Documentary credit amendments and presentation",
    ),
    "guarantee": ("guarantee_instruction", "Guarantee issuance, release and collateral"),
    "forward": ("forward_instruction", "Deliverable forward settlement and closeout"),
    "debit": ("direct_debit_instruction", "Direct debit mandate and refund administration"),
    "representment": ("representment_instruction", "Merchant chargeback defense and concessions"),
    "freight": ("cargo_title_instruction", "Freight liens, cargo title and delivery orders"),
    "rolling_reserve": ("reserve_cohort_instruction", "Marketplace reserve cohorts and refunds"),
    "netting": ("netting_instruction", "Multilateral novation and settlement"),
    "collateral": ("collateral_instruction", "Secured facility margin and substitution"),
    "insurance": ("marine_claim_instruction", "Marine insurance layers and recoveries"),
    "term_deposit": ("term_deposit_instruction", "Deposit maturity, breaking and withholding"),
    "export_license": (
        "export_license_instruction",
        "Export license quantity allocation and declarations",
    ),
}
NEW = {}
for k, (n, d) in NAMES.items():
    name = "propose_" + n
    NEW[name] = dict(
        name=name,
        description=d
        + ". Creates an immutable request; customer approval and execute_request perform one local action. See the applicable Trade Services contract for action values and fields.",
        parameters={"case_id": "string", "action": "string", "data": "object"},
        rule_id="TRADE-" + k,
    )
NEW["propose_service_inquiry"] = dict(
    name="propose_service_inquiry",
    description="Request an external source receipt for a service record. Creates an approved-execution request; the public receipt is delivered by a logical event. The receipt states external facts, not a recommended plan.",
    parameters={"case_id": "string", "record_id": "string"},
    rule_id="TRADE-common",
)
NEW["propose_service_payment"] = dict(
    name="propose_service_payment",
    description="Pay a named service obligation using its specified wallet. Bank authorization does not certify the amount remaining under commercial documents.",
    parameters={"case_id": "string", "record_id": "string", "amount_minor": "integer"},
    rule_id="TRADE-common",
)
CAPABILITIES = {**INHERITED, **NEW}
