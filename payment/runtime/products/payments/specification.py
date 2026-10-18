from copy import deepcopy
from payment.runtime.products.resources.specification import (
    CAPABILITIES as PREVIOUS,
    TABLES as PREVIOUS_TABLES,
)

EXTRA_TABLES = [
    "desk_portfolios",
    "trade_invoices",
    "remittances",
    "remittance_advice",
    "invoice_allocations",
    "payment_batch_items",
    "submission_attempts",
    "bank_receipts",
    "tax_transfers",
    "tax_periods",
    "message_amendments",
    "tax_allocations",
    "company_profiles",
    "ownership_edges",
    "onboarding_applications",
    "evidence_bundles",
    "rail_profiles",
    "desk_accounts",
    "rail_sites",
    "rail_bindings",
    "treasury_mandates",
    "treasury_obligations",
    "desk_payments",
    "card_loss_cases",
    "loss_components",
    "compensation_entries",
    "card_disputes",
    "restricted_lots",
    "fund_restrictions",
    "receivable_rights",
    "purpose_instructions",
    "desk_services",
    "billing_instances",
    "fx_routes",
    "fx_quotes",
    "fx_trades",
    "currency_obligations",
    "desk_journal",
    "reserve_routes",
    "reserve_disbursements",
]
TABLES = list(dict.fromkeys(list(PREVIOUS_TABLES) + EXTRA_TABLES))


def capability(rule, description, **parameters):
    return dict(description=description, parameters=parameters, rule_id=rule)


NEW_CAPABILITIES = {
    "propose_remittance_advice": capability(
        "DESK-reconciliation",
        "Request an authoritative remittance advice revision. A trace request is not confirmation of invoice allocation.",
        remittance_id="string",
    ),
    "propose_invoice_allocation": capability(
        "DESK-reconciliation",
        "Replace the reversible invoice allocation of one posted remittance, recording the advice used. Does not alter bank cash.",
        remittance_id="string",
        advice_id="string",
        allocations="object",
    ),
    "propose_receivable_close": capability(
        "DESK-reconciliation",
        "Close a receivables portfolio and freeze its allocations. Accepted closure does not certify customer intent or full reconciliation.",
        portfolio_id="string",
    ),
    "propose_attempt_trace": capability(
        "DESK-batches",
        "Request the bank receipt for a submission attempt whose acknowledgement is pending.",
        attempt_id="string",
    ),
    "propose_batch_recipient_update": capability(
        "DESK-batches",
        "Update the recipient version of one unpaid batch item from approved bank evidence. Does not cancel previously transmitted instructions.",
        item_id="string",
        submission_id="string",
    ),
    "propose_batch_item_payment": capability(
        "DESK-batches",
        "Submit the amount of a batch item with an explicit bank version and idempotency key. A new key can create another payment; the caller must establish whether another submission is needed.",
        item_id="string",
        bank_version="integer",
        submission_key="string",
    ),
    "propose_bank_message_amendment": capability(
        "DESK-tax",
        "Request a reference and tax-period amendment for an amendable bank transfer. Its recipient and principal are immutable.",
        transfer_id="string",
        tax_period="string",
        tax_reference="string",
    ),
    "propose_tax_allocation": capability(
        "DESK-tax",
        "Ask the tax authority to assign an existing payment to explicit period amounts using approved evidence. No new cash payment is made.",
        transfer_id="string",
        allocations="object",
        submission_id="string",
    ),
    "propose_onboarding_bundle": capability(
        "DESK-onboarding",
        "Assemble a versioned set of assessed evidence for an application. submission_ids is an object with submission IDs as keys and true as values.",
        application_id="string",
        submission_ids="object",
    ),
    "propose_onboarding_submission": capability(
        "DESK-onboarding",
        "Submit one assembled evidence bundle for independent institution review. The review may request additional current evidence.",
        bundle_id="string",
    ),
    "propose_desk_account_opening": capability(
        "DESK-rails",
        "Open an Operations Desk collection account under an available product for the stated legal entity. Opening is separate from platform acceptance.",
        profile_id="string",
        entity_id="string",
    ),
    "propose_rail_binding": capability(
        "DESK-rails",
        "Submit an explicit site, collection account and payment rail to platform validation. A saved binding is not an operational collection path.",
        site_id="string",
        account_id="string",
        rail="string",
    ),
    "propose_payout_validation": capability(
        "DESK-rails",
        "Initiate the platform test settlement of an accepted binding. Examine the actual test receipt before relying on that path.",
        binding_id="string",
    ),
    "propose_treasury_mandate": capability(
        "DESK-treasury",
        "Lodge an institution mandate from assessed signed evidence. Permissions become effective only after the recorded review.",
        mandate_id="string",
        submission_id="string",
    ),
    "propose_treasury_payment": capability(
        "DESK-treasury",
        "Pay from an account under one effective institutional mandate. The executor, payer, purpose, beneficiary and amount must fit that mandate.",
        obligation_id="string",
        account_id="string",
        mandate_id="string",
        executor_id="string",
        amount_minor="integer",
    ),
    "propose_card_dispute": capability(
        "DESK-disputes",
        "Open a card dispute with explicit loss-component amounts and assessed delivery evidence. Provisional registration does not establish final compensable loss.",
        case_id="string",
        claims="object",
        submission_id="string",
    ),
    "propose_dispute_adjustment": capability(
        "DESK-disputes",
        "Replace amounts of an unresolved dispute. Claims are per loss component; this does not undo refunds or settled compensation.",
        dispute_id="string",
        claims="object",
    ),
    "propose_dispute_finalization": capability(
        "DESK-disputes",
        "Submit an unresolved dispute for final settlement at its published review time. After finalization its claim amounts cannot be changed.",
        dispute_id="string",
    ),
    "propose_restriction_review": capability(
        "DESK-restrictions",
        "Submit evidence to lift one specified restriction. Other restrictions and assigned receivable rights remain in force.",
        restriction_id="string",
        submission_id="string",
    ),
    "propose_receivable_release": capability(
        "DESK-restrictions",
        "Apply assessed assignee evidence to release the specifically named receivable lots, after institution review.",
        right_id="string",
        submission_id="string",
    ),
    "propose_purpose_payment": capability(
        "DESK-restrictions",
        "Pay a purpose instruction using an explicit map of eligible source-lot amounts. Different instructions can consume the same available lots over time.",
        instruction_id="string",
        allocations="object",
    ),
    "propose_service_termination": capability(
        "DESK-exit",
        "Terminate a subscription after its recorded notice interval. Accepted billing instances remain payable on their original account.",
        service_id="string",
    ),
    "propose_billing_migration": capability(
        "DESK-exit",
        "Move future billing for one service to another open account of the same entity. Previously accepted instances keep their original account.",
        service_id="string",
        account_id="string",
    ),
    "propose_account_sweep": capability(
        "DESK-exit",
        "Move an explicit amount between same-entity, same-currency Desk accounts. This does not migrate accepted bills or expected returns.",
        source_account_id="string",
        destination_account_id="string",
        amount_minor="integer",
    ),
    "propose_desk_account_close": capability(
        "DESK-exit",
        "Permanently close an open zero-balance Desk account. Closure does not cancel previously accepted obligations or redirect returns.",
        account_id="string",
    ),
    "inspect_multicurrency_quote": capability(
        "DESK-fx",
        "Quote a specified source amount on a published currency path, including segment rounding and fee currencies. A quote does not reserve funds.",
        account_id="string",
        route_id="string",
        source_minor="integer",
    ),
    "propose_fx_execution": capability(
        "DESK-fx",
        "Execute an unexpired exact source-amount FX quote. All source and fee currencies are debited, and the actual target amount is credited after settlement.",
        quote_id="string",
    ),
    "propose_currency_payment": capability(
        "DESK-fx",
        "Deliver an explicit net amount for a currency obligation, deducting the published delivery fee from the same currency wallet.",
        obligation_id="string",
        account_id="string",
        net_minor="integer",
    ),
}
NEW_CAPABILITIES["propose_batch_residual_payment"] = capability(
    "DESK-batches",
    "Submit an explicit positive portion of a batch item principal with an immutable bank version and idempotency key. Aggregate supplier obligation limits remain the caller responsibility.",
    item_id="string",
    bank_version="integer",
    submission_key="string",
    amount_minor="integer",
)
NEW_CAPABILITIES["propose_reserve_disbursement"] = capability(
    "DESK-collateral",
    "Disburse one published reserve option after its local release conditions are met. Debit the full reserve charge, deliver the stated net amount and book any withheld fee. This does not check the customer overall funding plan.",
    route_id="string",
)
CAPABILITIES = {**deepcopy(PREVIOUS), **NEW_CAPABILITIES}
