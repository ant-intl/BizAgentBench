from copy import deepcopy
from payment.runtime.products.trade.specification import CAPABILITIES as OLD, SPECS as OLD_SPECS

SPECS = deepcopy(OLD_SPECS)
NAMES = {
    'payroll_attachment': ('payroll_attachment_instruction', 'Payroll attachments'),
    'dividend_grossup': ('dividend_grossup_instruction', 'Dividend withholding'),
    'collection_acceptance': ('collection_acceptance_instruction', 'Documentary collections'),
    'daily_windows': ('daily_windows_instruction', 'Payment window limits'),
    'value_date': ('value_date_instruction', 'Value-date adjustments'),
    'certificate_revocation': ('certificate_revocation_instruction', 'Certificate revocations'),
    'evidence_periods': ('evidence_periods_instruction', 'Statement coverage'),
    'late_presentment': ('late_presentment_instruction', 'Late card presentments'),
    'currency_refund': ('currency_refund_instruction', 'Card refund currencies'),
    'expense_classification': ('expense_classification_instruction', 'Expense reimbursements'),
    'successor_refund': ('successor_refund_instruction', 'Successor refunds'),
    'merger_eligibility': ('merger_eligibility_instruction', 'Merger account eligibility'),
    'dual_approval': ('dual_approval_instruction', 'Independent signatories'),
    'approval_tolerance': ('approval_tolerance_instruction', 'Approval tolerance bands'),
    'guaranteed_net': ('guaranteed_net_instruction', 'Guaranteed net remittances'),
    'billing_plan': ('billing_plan_instruction', 'Service billing plans'),
    'data_scope': ('data_scope_instruction', 'Marketplace data scopes'),
    'demurrage': ('demurrage_instruction', 'Container appointments'),
    'early_discount': ('early_discount_instruction', 'Early payment discounts'),
    'interest_prepayment': ('interest_prepayment_instruction', 'Loan prepayment allocation'),
}
NEW = {
    "propose_"
    + n: dict(
        name="propose_" + n,
        description=title
        + ". Prepare one local action, subject to customer approval and bank execution. The service policy states the supported actions and their fields.",
        parameters={"case_id": "string", "action": "string", "data": "object"},
        rule_id="EXT-" + k,
    )
    for k, (n, title) in NAMES.items()
}
CAPABILITIES = {**OLD, **NEW}
