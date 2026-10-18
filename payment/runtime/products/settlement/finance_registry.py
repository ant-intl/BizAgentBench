from payment.runtime.products.settlement.models import Mechanism

REGISTRY = (
    Mechanism(
        "jv_default_equalization",
        "propose_finance_jv",
        "Prepare a member rescue contribution, source-specific bridge return, creditor novation, residual debt assignment or project payment.",
        "finance_jv",
        "FinanceOperations",
        "jv_instruction",
        "jv_default_equalization_event",
        "jv_event",
    ),
    Mechanism(
        "syndicated_transfer_allocation",
        "propose_finance_syndicated",
        "Prepare transfer support or accelerated consent settlement, a lender-specific machinery draw contribution, actual delivery payment, or separate final principal and period-fee distribution.",
        "finance_syndicated",
        "FinanceOperations",
        "syndicated_instruction",
        "syndicated_transfer_allocation_event",
        "syndicated_event",
    ),
    Mechanism(
        "borrowing_base_retirement",
        "propose_finance_borrowing",
        "Prepare a facility draw or repayment, legacy retirement, receivable sale, debtor-source collection application or release, supplier credit application, or inventory payment.",
        "finance_borrowing",
        "FinanceOperations",
        "borrowing_instruction",
        "borrowing_base_retirement_event",
        "borrowing_event",
    ),
)
