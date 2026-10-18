from copy import deepcopy
from payment.runtime.products.review.specification import SPECS as OLD_SPECS, CAPABILITIES as OLD

NAMES = {
    "diagnostic_debit": (
        "collection_investigation",
        "Duplicate collection investigation and mandate control",
    ),
    "succession_refund": (
        "corporate_entitlement",
        "Corporate succession, refund rights and account continuity",
    ),
    "collateral_portfolio": ("collateral_reallocation", "Security substitution and loan repayment"),
    "layered_recovery": ("insurance_recovery", "Layered cargo recovery and replacement payments"),
    "ledger_resume": (
        "ledger_recovery",
        "Interrupted sales ledger imports and settlement reconciliation",
    ),
    "cash_calendar": (
        "deposit_calendar",
        "Deposit redemption, tax declarations and cash obligations",
    ),
    "net_recourse": ("net_remittance", "Guaranteed remittance and residual obligation settlement"),
    "discount_portfolio": ("invoice_discounting", "Invoice discount settlement and credit notes"),
}
SPECS = deepcopy(OLD_SPECS)
NEW = {
    "propose_"
    + n: dict(
        name="propose_" + n,
        rule_id="DIVERSE-" + k,
        description=t
        + ". Prepares one local instruction; see the product document for action fields and effects.",
        parameters={"case_id": "string", "action": "string", "data": "object"},
    )
    for k, (n, t) in NAMES.items()
}
CAPABILITIES = {**OLD, **NEW}
