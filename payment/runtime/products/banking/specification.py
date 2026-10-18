"""Public synthetic parameters. No task contracts or successful plans live here."""

PARAMS = {
    "conversion_rate": 7,
    "conversion_delay": 10,
    "payment_delay": 10,
    "activation_delay": 20,
    "cutover_delay": 20,
    "review_delay": 30,
    "bank_sync_delay": 20,
    "platform_review_delay": 20,
    "quote_validity": 20,
    "conversion_fee": 100,
    "export_payment_fee": 100,
    "restricted_payment_fee": 300,
    "bank_payment_fee": 500,
    "card_payment_fee": 500,
    "ownership_minimum": 50,
    "shared_r90_minimum": 15000000,
    "plans": {
        "STANDARD": {"activation_fee": 0, "rate_bp": 40, "minimum_minor": 100, "delay": 0},
        "PRO": {"activation_fee": 2000, "rate_bp": 10, "minimum_minor": 100, "delay": 30},
        "SHARED": {"activation_fee": 0, "rate_bp": 15, "minimum_minor": 200, "delay": 5},
    },
}

# These are real callable capabilities, common to every scenario. The hidden
# evaluator never selects which tools a participant may discover.
CAPABILITIES = {}


def capability(name, topic, description, **fields):
    CAPABILITIES[name] = {
        "name": name,
        "topic": topic,
        "description": description,
        "parameters": fields,
        "read": not name.startswith("propose_"),
    }


capability(
    "assess_evidence",
    "evidence",
    "Assess the actual uploaded file against its target and declared purpose. Submission is not approval.",
    submission_id="string",
)
capability(
    "propose_account_activation",
    "accounts",
    "Propose activation of an already provisioned collection account.",
    account_id="string",
)
capability(
    "propose_platform_cutover",
    "migration",
    "Change future platform routing and store ownership after account activation and ownership evidence.",
    store_id="string",
    account_id="string",
    submission_id="string",
)
capability(
    "propose_account_retirement",
    "accounts",
    "Close an open zero-balance account. Closure does not redirect already transmitted funds.",
    account_id="string",
)
capability(
    "propose_balance_transfer",
    "migration",
    "Move a specified credited USD balance between accessible accounts. Source restrictions survive transfers.",
    source_id="string",
    destination_id="string",
    amount_minor="integer",
)
capability(
    "inspect_conversion",
    "conversion",
    "Obtain an executable USD-to-CNY quote for a specified fund lot and quota allocation.",
    lot_id="string",
    amount_minor="integer",
    quota_allocations="object",
)
capability(
    "propose_conversion",
    "conversion",
    "Propose execution of one unexpired conversion quote.",
    quote_id="string",
)
capability(
    "propose_cny_withdrawal",
    "payments",
    "Send CNY from an account to an existing verified CNY beneficiary.",
    account_id="string",
    beneficiary_id="string",
    amount_minor="integer",
)
capability(
    "inspect_lot_payment",
    "provenance",
    "Quote a USD payment against a specified source lot. Quotes do not reserve availability.",
    lot_id="string",
    beneficiary_id="string",
    amount_minor="integer",
)
capability(
    "propose_lot_payment",
    "provenance",
    "Propose execution of one unexpired fund-lot payment quote.",
    quote_id="string",
)
capability(
    "propose_bank_trace",
    "tracing",
    "Request independent bank advice for a credited incoming transfer.",
    incoming_id="string",
)
capability(
    "propose_reservation_cancellation",
    "quota",
    "Cancel an open reservation and release its live resource occupancy. Expiry is not reversed.",
    reservation_id="string",
)
capability(
    "propose_credit_reversal",
    "corrections",
    "Reverse a credited incoming and its derived quota after occupied funds are released.",
    incoming_id="string",
)
capability(
    "propose_credit_assignment",
    "corrections",
    "Assign an unapplied or reversed incoming to an accessible open account. Operator must establish the correct ownership.",
    incoming_id="string",
    account_id="string",
)
capability(
    "propose_beneficiary_update",
    "beneficiaries",
    "Apply current verified bank details; the beneficiary version increments after review.",
    beneficiary_id="string",
    submission_id="string",
)
capability(
    "inspect_bank_payment",
    "payments",
    "Quote a USD payment for an obligation, a beneficiary version and an accessible account.",
    account_id="string",
    beneficiary_id="string",
    obligation_id="string",
    amount_minor="integer",
)
capability(
    "propose_bank_payment",
    "payments",
    "Propose execution of a bank payment quote. Existing transfers are not automatically cancelled.",
    quote_id="string",
)
capability(
    "propose_request_cancellation",
    "consent",
    "Cancel an unexecuted customer request. Cancellation does not cancel a sent payment.",
    request_id="string",
)
capability(
    "propose_operator_role",
    "permissions",
    "Grant PAY to an already accepted active operator after a review delay.",
    operator_id="string",
)
capability(
    "propose_operator_revocation",
    "permissions",
    "Revoke an operator and increment its authorization epoch. Earlier approvals from it become invalid.",
    operator_id="string",
)
capability(
    "propose_authorized_disbursement",
    "permissions",
    "Prepare a specific obligation payment requiring an active PAY operator at approval and execution.",
    account_id="string",
    beneficiary_id="string",
    obligation_id="string",
    amount_minor="integer",
)
capability(
    "inspect_card_authorizations",
    "cards",
    "Read active and historical authorizations for one card, including actual ledger occupancy.",
    card_id="string",
)
capability(
    "propose_authorization_void",
    "cards",
    "Ask the merchant to void one authorization. Merchant acknowledgement determines whether occupancy is released.",
    authorization_id="string",
)
capability(
    "propose_card_funding_source",
    "cards",
    "Change the account used for new card charges. Existing holds keep their original account.",
    card_id="string",
    account_id="string",
)
capability(
    "propose_card_purchase",
    "cards",
    "Pay a specified existing merchant invoice using the card current funding account.",
    card_id="string",
    obligation_id="string",
    amount_minor="integer",
)
capability(
    "propose_bank_identity_sync",
    "identity",
    "Synchronize one bank account profile to the current registered legal identity.",
    account_id="string",
)
capability(
    "issue_bank_letter",
    "letters",
    "Issue an immutable account certificate describing the bank profile at issue time.",
    account_id="string",
)
capability(
    "propose_platform_reverification",
    "platform",
    "Submit a bank letter and current registry evidence for platform account review. An optional translation submission may be supplied.",
    store_id="string",
    letter_id="string",
    submission_id="string",
    translation_submission_id="string?",
)
capability(
    "propose_order_refund",
    "refunds",
    "Refund a recorded sale to its original payer; the order quota is reduced at completion even if already consumed.",
    order_id="string",
)
capability(
    "propose_sales_import",
    "sales",
    "Import eligible, nonduplicate sales from an approved current statement; records generate quota, not cash.",
    account_id="string",
    submission_id="string",
)
capability(
    "inspect_plan_charge",
    "pricing",
    "Quote a specified invoice using the currently effective plan and eligibility. This is not a future-cost optimizer.",
    account_id="string",
    obligation_id="string",
)
capability(
    "propose_plan_switch",
    "pricing",
    "Activate a selected published plan. Charges already incurred and activation charges are not retroactively refunded.",
    account_id="string",
    plan="string",
)
capability(
    "propose_priced_payment",
    "pricing",
    "Pay an existing eligible invoice under the plan effective at execution time.",
    account_id="string",
    obligation_id="string",
    amount_minor="integer",
)
capability(
    "propose_batch_review",
    "batches",
    "Submit binding evidence and remittance evidence for the same sampled transfer. Review can release its linked batch in stages.",
    batch_id="string",
    binding_submission_id="string",
    remittance_submission_id="string",
)
capability(
    "inspect_resource_occupancy",
    "occupancy",
    "Inspect current holds and reservations on an account; the result does not forecast a complete payment plan.",
    account_id="string",
)
capability(
    "propose_collection_application",
    "accounts",
    "Apply for a Collection Profile account matching an accessible legal entity registration region. Profile account terms are separate from CORE.",
    entity_id="string",
    profile_id="string",
)
capability(
    "inspect_profile_pricing",
    "pricing",
    "Quote the current Collection Profile payout fee for an account and amount in its settlement currency.",
    account_id="string",
    amount_minor="integer",
)
capability(
    "propose_profile_payout",
    "payments",
    "Pay from a Collection Profile account in its settlement currency, enforcing its per-payment minimum and daily principal cap.",
    account_id="string",
    beneficiary_id="string",
    amount_minor="integer",
)

SOURCES = {
    "migration": "Source materials/SOP/Store migration and account services./05_Currency account-Currency account migration query.md",
    "accounts": "Source materials/SOP/Store migration and account services./09_Termination_Cancel account.md",
    "provenance": "Source materials/SOP/Receipts, payments, and currency accounts/17_Limit acquisition issue-FX settlement quota consultation.md",
    "conversion": "Source materials/SOP/Receipts, payments, and currency accounts/03_RMB&Foreign currency transaction management-Withdrawal_Transfer operation consultation.md",
    "corrections": "Source materials/SOP/Receipts, payments, and currency accounts/05_Incoming payment on hold_Misaligned posting.md",
    "tracing": "Source materials/SOP/Store authorization and account security/15_Payment not received.md",
    "beneficiaries": "Source materials/SOP/Receipts, payments, and currency accounts/15_Manage payee inquiries-Payee modification and verification inquiry.md",
    "payments": "Source materials/SOP/Receipts, payments, and currency accounts/06_Foreign currency transaction management-Payment returned by bank and repaid.md",
    "permissions": "Source materials/SOP/Store authorization and account security/17_Manage account security information.md",
    "cards": "Source materials/SOP/Receipts, payments, and currency accounts/09_Card transaction management-Deduction-related inquiries.md",
    "identity": "Source materials/SOP/Receipts, payments, and currency accounts/07_Account information inquiry - Incoming payment reminders, name changes, etc..md",
    "letters": "Source materials/SOP/Receipts, payments, and currency accounts/19_Apply for an account certification letter.md",
    "platform": "Source materials/SOP/Store authorization and account security/13_Amazon platform authorization obtains foreign exchange settlement quota.md",
    "quota": "Source materials/SOP/Receipts, payments, and currency accounts/17_Limit acquisition issue-FX settlement quota consultation.md",
    "sales": "Source materials/SOP/Receipts, payments, and currency accounts/18_Limit acquisition issue-Upload transaction details.md",
    "refunds": "Source materials/SOP/Receipts, payments, and currency accounts/18_Limit acquisition issue-Upload transaction details.md",
    "pricing": "Source materials/SOP/Store authorization and account security/16_Rate standard query..md",
    "batches": "Source materials/SOP/Receipts, payments, and currency accounts/05_Incoming payment on hold_Misaligned posting.md",
    "occupancy": "Source materials/SOP/Receipts, payments, and currency accounts/09_Card transaction management-Deduction-related inquiries.md",
    "consent": None,
    "evidence": None,
    "protocol": None,
}

# Published commercial mechanisms used by the third release.
PARAMS["frontier"] = {
    "advance_fee_minor": 800,
    "covenant_breach_fee_minor": 2000,
    "source_review_delay": 5,
    "source_review_fee_minor": 100,
    "allocation_amendment_fee_minor": 200,
    "cash_discount_minor": 10000,
    "cash_discount_deadline": 40,
    "operator_limits": {"AP_NORTH": 500000, "AP_SOUTH": 400000},
    "source_policies": {
        "MARKET_A": {"convertible": True, "beneficiary_segments": ["GENERAL"], "fee_minor": 100},
        "MARKET_B": {
            "convertible": True,
            "beneficiary_segments": ["GENERAL", "LICENSED"],
            "fee_minor": 100,
        },
        "SERVICE": {"convertible": False, "beneficiary_segments": ["GENERAL"], "fee_minor": 200},
        "CAPITAL": {"convertible": False, "beneficiary_segments": ["LICENSED"], "fee_minor": 200},
        "REIMBURSEMENT": {
            "convertible": False,
            "beneficiary_segments": ["GENERAL", "LICENSED"],
            "fee_minor": 300,
        },
    },
    "tariffs": {
        "BASIC": {
            "activation_minor": 0,
            "bp": 40,
            "minimum_minor": 100,
            "threshold_minor": 0,
            "upper_bp": 40,
        },
        "VOLUME": {
            "activation_minor": 2400,
            "bp": 12,
            "minimum_minor": 150,
            "threshold_minor": 1000000,
            "upper_bp": 6,
        },
        "PARTNER": {
            "activation_minor": 0,
            "bp": 15,
            "minimum_minor": 200,
            "threshold_minor": 0,
            "upper_bp": 15,
        },
        "COMMIT": {
            "activation_minor": 4500,
            "bp": 4,
            "minimum_minor": 400,
            "threshold_minor": 0,
            "upper_bp": 4,
        },
    },
    "tariff_slots": [0, 30, 60],
    "commit_rebate_minor": 3000,
    "commit_qualifying_minimum_minor": 3000000,
}
capability(
    "propose_receivable_advance",
    "liquidity",
    "Finance an identified transmitted receivable into a controlled-group accessible account. Principal is recovered from that receivable on arrival.",
    incoming_id="string",
    destination_id="string",
    amount_minor="integer",
)
capability(
    "inspect_source_payment",
    "source_routes",
    "Quote one recipient payment with an explicit allocation across eligible source lots.",
    beneficiary_id="string",
    allocations="object",
)
capability(
    "propose_source_payment",
    "source_routes",
    "Execute a current source-allocation quote; source classifications and current availability apply.",
    quote_id="string",
)
capability(
    "propose_credit_note_allocation",
    "credits",
    "Apply available commercial credit to one eligible invoice. Applied credit is not cash and is not refundable through this operation.",
    note_id="string",
    obligation_id="string",
    amount_minor="integer",
)
capability(
    "propose_invoice_cash_discount",
    "credits",
    "Register an invoice cash-settlement concession if its recorded terms remain eligible.",
    obligation_id="string",
)
capability(
    "propose_capture_refund",
    "card_adjustments",
    "Request reimbursement of a captured card amount identified by merchant advice.",
    authorization_id="string",
)
capability(
    "issue_attested_bank_letter",
    "attestations",
    "Issue an immutable letter from the bank profile to a named addressee.",
    account_id="string",
    addressee="string",
)
capability(
    "propose_platform_intake",
    "attestations",
    "Register a platform review case within its published intake window using current registry evidence.",
    store_id="string",
    submission_id="string",
)
capability(
    "propose_conversion_allocation_amendment",
    "quota_history",
    "Replace the quota allocation of an existing settled conversion without changing its cash leg.",
    payment_id="string",
    quota_allocations="object",
)
capability(
    "propose_tariff_enrolment",
    "tariffs",
    "Register a tariff to start at a published billing slot. Replacing a pending instruction is permitted; posted charges remain.",
    account_id="string",
    plan="string",
    slot_minute="integer",
)
capability(
    "inspect_tariff_quote",
    "tariffs",
    "Quote a single payable invoice under the tariff and tier currently effective.",
    account_id="string",
    obligation_id="string",
)
capability(
    "propose_tariff_payment",
    "tariffs",
    "Submit one whole commercial invoice under current tariff terms.",
    account_id="string",
    obligation_id="string",
)
capability(
    "propose_tariff_rebate",
    "rebates",
    "Claim the available fixed contractual rebate on settled qualifying invoices in the current period.",
    account_id="string",
)
capability(
    "propose_source_reclassification",
    "classification",
    "Submit source evidence for a specified portion of an existing settlement lot. Approved reclassification creates a separate traceable lot.",
    lot_id="string",
    amount_minor="integer",
    submission_id="string",
)
for _topic, _source in {
    "liquidity": SOURCES["migration"],
    "source_routes": SOURCES["provenance"],
    "credits": SOURCES["payments"],
    "card_adjustments": SOURCES["cards"],
    "attestations": SOURCES["letters"],
    "quota_history": SOURCES["quota"],
    "tariffs": SOURCES["pricing"],
    "rebates": SOURCES["pricing"],
    "classification": SOURCES["batches"],
    "assignments": SOURCES["corrections"],
    "delegated_limits": SOURCES["permissions"],
}.items():
    SOURCES[_topic] = _source
