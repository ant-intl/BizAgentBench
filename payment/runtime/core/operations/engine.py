"""Ordered business handlers for the Operations Desk product.

Unique actions dispatch directly. Shared lifecycle hooks explicitly continue
through the listed providers using next_handler(). No class inheritance or
import-time method replacement participates in business execution.
"""

from payment.runtime.core.dispatch import HandlerRegistry, BusinessRuntime
from payment.runtime.products.dependencies.engine import WorkflowLinks
from payment.runtime.products.joint_funding.engine import JointFunding
from payment.runtime.products.lifecycle.engine import PaymentLifecycle
from payment.runtime.products.settlement.engine import SettlementServices
from payment.runtime.products.settlement.custody_coupon_engine import CustodyCouponOperations
from payment.runtime.products.settlement.custody_election_engine import CustodyElectionOperations
from payment.runtime.products.settlement.custody_ndf_engine import CustodyNdfOperations
from payment.runtime.products.settlement.customs_engine import CustomsOperations
from payment.runtime.products.settlement.finance_engine import FinanceOperations
from payment.runtime.products.settlement.platform_engine import PlatformOperations
from payment.runtime.products.settlement.security_engine import CredentialOperations
from payment.runtime.products.settlement.security_engine import FraudOperations
from payment.runtime.products.settlement.security_engine import RetentionOperations
from payment.runtime.products.settlement.settlement_engine import PvPOperations
from payment.runtime.products.settlement.settlement_engine import DefaultOperations
from payment.runtime.products.settlement.settlement_engine import DefaultOperationsBase
from payment.runtime.products.settlement.settlement_engine import AssignmentOperations
from payment.runtime.products.settlement.settlement_engine import AssignmentOperationsBase
from payment.runtime.products.settlement.settlement_engine import PoolOperations
from payment.runtime.products.settlement.settlement_engine import PoolOperationsBase
from payment.runtime.products.services.engine import BusinessServices
from payment.runtime.products.services.entitlement_engine import EntitlementOperations
from payment.runtime.products.services.expense_engine import ExpenseOperations
from payment.runtime.products.services.debit_engine import DebitOperations
from payment.runtime.products.services.certificate_engine import CertificateOperations
from payment.runtime.products.services.insurance_engine import InsuranceOperations
from payment.runtime.products.services.billing_engine import BillingOperations
from payment.runtime.products.services.payroll_engine import PayrollOperations
from payment.runtime.products.services.sync_engine import SyncOperations
from payment.runtime.products.services.authority_engine import AuthorityOperations
from payment.runtime.products.services.forward_engine import ForwardOperations
from payment.runtime.products.services.escrow_engine import EscrowOperations
from payment.runtime.products.services.license_engine import LicenseOperations
from payment.runtime.products.services.credit_engine import CreditOperations
from payment.runtime.products.services.handling_engine import HandlingOperations
from payment.runtime.products.services.consolidation_engine import ConsolidationOperations
from payment.runtime.products.services.factoring_engine import FactoringOperations
from payment.runtime.products.services.netting_engine import NettingOperations
from payment.runtime.products.services.interest_engine import InterestOperations
from payment.runtime.products.services.presentment_engine import PresentmentOperations
from payment.runtime.products.services.distribution_engine import DistributionOperations
from payment.runtime.products.services.freight_title_engine import FreightTitleOperations
from payment.runtime.products.services.discount_engine import DiscountOperations
from payment.runtime.products.services.representment_engine import RepresentmentOperations
from payment.runtime.products.services.collection_engine import CollectionOperations
from payment.runtime.products.services.prepayment_engine import PrepaymentOperations
from payment.runtime.products.services.guarantee_engine import GuaranteeOperations
from payment.runtime.products.services.currency_refund_engine import CurrencyRefundOperations
from payment.runtime.products.services.tolerance_engine import ToleranceOperations
from payment.runtime.products.services.daily_windows_engine import DailyWindowsOperations
from payment.runtime.products.services.deposit_engine import DepositOperations
from payment.runtime.products.services.evidence_periods_engine import EvidencePeriodsOperations
from payment.runtime.products.services.guaranteed_net_engine import GuaranteedNetOperations
from payment.runtime.products.services.reserve_engine import ReserveOperations
from payment.runtime.products.portfolio.engine import PortfolioOperations
from payment.runtime.products.funding.engine import FundingProgrammes
from payment.runtime.products.review.engine import EvidenceSettlement
from payment.runtime.products.payment_orders.engine import PaymentServices
from payment.runtime.products.trade.engine import TradeServices
from payment.runtime.products.evidence.engine import EvidenceExecution
from payment.runtime.products.constraints.engine import CustomerConstraints
from payment.runtime.products.payments.engine import LinkedSettlement
from payment.runtime.products.payments.engine import ResidualPayments
from payment.runtime.products.payments.engine import CustomerScope
from payment.runtime.products.payments.engine import DeskPayments
from payment.runtime.products.queries.engine import ResourceQueries
from payment.runtime.products.resources.engine import ResourceOperations
from payment.runtime.products.credit.engine import CreditAndAuthority
from payment.runtime.products.customer_access.engine import CustomerPortal
from payment.runtime.products.banking.engine import BankingOperations

REGISTRY = HandlerRegistry(
    (
        WorkflowLinks,
        JointFunding,
        PaymentLifecycle,
        SettlementServices,
        CustodyCouponOperations,
        CustodyElectionOperations,
        CustodyNdfOperations,
        CustomsOperations,
        FinanceOperations,
        PlatformOperations,
        CredentialOperations,
        FraudOperations,
        RetentionOperations,
        PvPOperations,
        DefaultOperations,
        DefaultOperationsBase,
        AssignmentOperations,
        AssignmentOperationsBase,
        PoolOperations,
        PoolOperationsBase,
        BusinessServices,
        EntitlementOperations,
        ExpenseOperations,
        DebitOperations,
        CertificateOperations,
        InsuranceOperations,
        BillingOperations,
        PayrollOperations,
        SyncOperations,
        AuthorityOperations,
        ForwardOperations,
        EscrowOperations,
        LicenseOperations,
        CreditOperations,
        HandlingOperations,
        ConsolidationOperations,
        FactoringOperations,
        NettingOperations,
        InterestOperations,
        PresentmentOperations,
        DistributionOperations,
        FreightTitleOperations,
        DiscountOperations,
        RepresentmentOperations,
        CollectionOperations,
        PrepaymentOperations,
        GuaranteeOperations,
        CurrencyRefundOperations,
        ToleranceOperations,
        DailyWindowsOperations,
        DepositOperations,
        EvidencePeriodsOperations,
        GuaranteedNetOperations,
        ReserveOperations,
        PortfolioOperations,
        FundingProgrammes,
        EvidenceSettlement,
        PaymentServices,
        TradeServices,
        EvidenceExecution,
        CustomerConstraints,
        LinkedSettlement,
        ResidualPayments,
        CustomerScope,
        DeskPayments,
        ResourceQueries,
        ResourceOperations,
        CreditAndAuthority,
        CustomerPortal,
        BankingOperations,
    )
)


class Engine(BusinessRuntime):
    def __init__(self, world):
        super().__init__(world, REGISTRY)
