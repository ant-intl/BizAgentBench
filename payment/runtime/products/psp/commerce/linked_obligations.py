"""Business ledgers and operations linked to merchant payment obligations.

Operations update related commitments, fund availability, and asynchronous facts.
"""

from copy import deepcopy

PRODUCTS = {
    "delivery_acceptances": (
        "delivery_acceptances",
        "manage_delivery_acceptance",
        "pay_screened_invoice",
    ),
    "supplier_credit_notes": (
        "supplier_credit_notes",
        "manage_supplier_credit_note",
        "pay_supplier_account",
    ),
    "card_authorizations": (
        "card_authorizations",
        "manage_card_authorization",
        "execute_card_purchase",
    ),
    "subscription_consents": (
        "subscription_consents",
        "manage_subscription_consent",
        "run_merchant_billing",
    ),
    "merchant_refund_cases": (
        "merchant_refund_cases",
        "manage_merchant_refund_case",
        "resolve_currency_dispute",
    ),
    "provisional_cash_claims": (
        "provisional_cash_claims",
        "manage_provisional_cash_claim",
        "settle_cash_shortfall",
    ),
    "carrier_fee_claims": (
        "carrier_fee_claims",
        "manage_carrier_fee_claim",
        "settle_carrier_collection",
    ),
    "tax_registrations": (
        "tax_registrations",
        "manage_tax_registration",
        "remit_order_sales_tax",
    ),
    "interaccount_matches": (
        "interaccount_matches",
        "manage_interaccount_match",
        "finalize_statement_day",
    ),
    "recovery_period_bindings": (
        "recovery_period_bindings",
        "manage_recovery_period_binding",
        "recover_payment_posting",
    ),
    "correction_components": (
        "correction_components",
        "manage_correction_components",
        "post_period_adjustment",
    ),
    "custody_bank_notices": (
        "custody_bank_notices",
        "manage_custody_bank_notice",
        "settle_protected_customer",
    ),
    "channel_business_keys": (
        "channel_business_keys",
        "manage_channel_business_key",
        "send_channel_replacement",
    ),
    "reservation_commitments": (
        "reservation_commitments",
        "manage_reservation_commitment",
        "execute_reserved_payment",
    ),
    "receivable_disputes": (
        "receivable_disputes",
        "manage_receivable_dispute",
        "collect_committed_receivable",
    ),
    "beneficiary_delivery_terms": (
        "beneficiary_delivery_terms",
        "manage_beneficiary_delivery_terms",
        "commit_payment_route",
    ),
    "wallet_purchase_orders": (
        "wallet_purchase_orders",
        "manage_wallet_purchase_order",
        "run_wallet_observation",
    ),
}
PUBLIC_ACTIONS = {
    "delivery_acceptances": [
        "inspect_goods",
        "request_replacement",
        "record_goods_acceptance",
        "select_collection_account",
        "set_warranty_amount",
    ],
    "supplier_credit_notes": [
        "retrieve_credit_statement",
        "apply_credit_note",
        "select_collection_account",
        "bind_remittance_reference",
        "reconcile_supplier_statement",
    ],
    "card_authorizations": [
        "retrieve_authorizations",
        "cancel_authorization",
        "assign_employee",
        "reserve_purchase_amount",
    ],
    "subscription_consents": [
        "retrieve_capture_orders",
        "dispose_capture",
        "record_service_consent",
        "bind_card_generation",
    ],
    "merchant_refund_cases": [
        "retrieve_refund_statement",
        "allocate_merchant_refund",
        "submit_conversion_evidence",
        "reconcile_refund_settlement",
    ],
    "provisional_cash_claims": [
        "retrieve_provisional_terms",
        "reconcile_presentment",
        "allocate_provisional_credit",
        "finalize_provisional_balance",
    ],
    "carrier_fee_claims": ["retrieve_carrier_fees", "settle_return_fee", "offset_commission", "allocate_remittance"],
    "tax_registrations": [
        "retrieve_jurisdiction_status",
        "register_jurisdiction",
        "reserve_tax_credit",
        "publish_credit_schedule",
    ],
    "interaccount_matches": [
        "retrieve_transfer_advice",
        "match_bank_reference",
        "post_consolidation_entry",
        "publish_consolidated_report",
    ],
    "recovery_period_bindings": [
        "retrieve_period_locks",
        "select_posting_period",
        "record_returned_payable",
        "publish_rollforward",
    ],
    "correction_components": [
        "retrieve_component_advice",
        "match_payment_reference",
        "recognize_posted_component",
        "publish_component_schedule",
    ],
    "custody_bank_notices": [
        "retrieve_custody_notice",
        "allocate_fee_owner",
        "settle_fee_allocation",
        "certify_coverage",
    ],
    "channel_business_keys": [
        "retrieve_instruction_register",
        "bind_business_keys",
        "set_cancelled_instruction_disposition",
        "acknowledge_deliveries",
    ],
    "reservation_commitments": [
        "retrieve_reservation_contracts",
        "set_live_commitment",
        "bind_supplier_currency",
        "release_commitment",
    ],
    "receivable_disputes": [
        "retrieve_debtor_confirmation",
        "set_collection_tranche",
        "set_disputed_claim_disposition",
        "allocate_collection_receipt",
    ],
    "beneficiary_delivery_terms": [
        "validate_beneficiary",
        "select_beneficiary_account",
        "obtain_delivery_quote",
        "set_fee_bearer",
        "reconcile_delivery",
    ],
    "wallet_purchase_orders": [
        "retrieve_merchant_order_status",
        "cancel_order",
        "reserve_order_amount",
        "set_clearing_priority",
        "reconcile_capture",
    ],
}


def canonical(product_kind, action, reference, amount, r):
    simple = {
        "delivery_acceptances": dict(
            inspect_goods="inspect_goods",
            request_replacement="replace_short_delivery",
            record_goods_acceptance="accept_goods",
            select_collection_account="qualify_account",
            set_warranty_amount="retain_warranty",
        ),
        "supplier_credit_notes": dict(
            retrieve_credit_statement="obtain_credit_statement",
            apply_credit_note="apply_credit",
            select_collection_account="check_collection_account",
            bind_remittance_reference="bind_invoice_reference",
            reconcile_supplier_statement="reconcile_supplier_balance",
        ),
        "card_authorizations": dict(
            retrieve_authorizations="obtain_authorization_status",
            cancel_authorization=(
                "cancel_void_authorization"
                if reference == r.get("void_authorization_reference")
                else "cancel_delivered_authorization"
            ),
            assign_employee="bind_employee_purchase",
            reserve_purchase_amount="reserve_required_purchase",
        ),
        "subscription_consents": dict(
            retrieve_capture_orders="obtain_capture_status",
            dispose_capture="void_unwanted_capture" if amount == 0 else "retain_unwanted_capture",
            record_service_consent=(
                "renew_service_consent" if amount > 0 else "cancel_service_consent"
            ),
            bind_card_generation="bind_current_credentials",
        ),
        "merchant_refund_cases": dict(
            retrieve_refund_statement="obtain_refund_statement",
            allocate_merchant_refund="match_refund_component",
            submit_conversion_evidence="submit_remaining_evidence",
            reconcile_refund_settlement="settle_refund_overlap",
        ),
        "provisional_cash_claims": dict(
            retrieve_provisional_terms="obtain_provisional_credit",
            reconcile_presentment="reconcile_presentment",
            allocate_provisional_credit="allocate_provisional_credit",
            finalize_provisional_balance=(
                "finalize_provisional_credit" if amount == 0 else "return_entire_provisional"
            ),
        ),
        "carrier_fee_claims": dict(
            retrieve_carrier_fees="obtain_carrier_fee_statement",
            settle_return_fee="contest_return_fee" if amount == 0 else "accept_return_fee",
            offset_commission="offset_contract_commission",
            allocate_remittance=(
                "allocate_parcel_remittance"
                if reference == r.get("invoice_batch_reference")
                else "allocate_as_advance"
            ),
        ),
        "tax_registrations": dict(
            retrieve_jurisdiction_status="obtain_jurisdiction_status",
            register_jurisdiction=(
                "register_destination" if reference == r.get("destination") else "register_origin"
            ),
            reserve_tax_credit=(
                "apply_same_jurisdiction_credit"
                if reference == r.get("credit_jurisdiction")
                else "apply_other_jurisdiction_credit"
            ),
            publish_credit_schedule=(
                "publish_tax_credit_schedule"
                if amount == r.get("credit_minor")
                else "forfeit_credit"
            ),
        ),
        "interaccount_matches": dict(
            retrieve_transfer_advice="obtain_transfer_reference",
            match_bank_reference="match_transfer_pair",
            post_consolidation_entry=(
                "post_interaccount_elimination"
                if reference == "internal_transfer"
                else "classify_as_sales"
            ),
            publish_consolidated_report="publish_consolidated_close",
        ),
        "recovery_period_bindings": dict(
            retrieve_period_locks="obtain_period_locks",
            select_posting_period=(
                "bind_open_period"
                if reference == r.get("open_period")
                else "reopen_historical_period"
            ),
            record_returned_payable=(
                "recognize_returned_payable" if amount else "write_off_returned_payable"
            ),
            publish_rollforward="publish_recovery_rollforward",
        ),
        "correction_components": dict(
            retrieve_component_advice="obtain_component_advice",
            match_payment_reference="match_prior_payment",
            recognize_posted_component=(
                "recognize_existing_component"
                if amount == r.get("existing_component_minor")
                else "duplicate_existing_component"
            ),
            publish_component_schedule="publish_component_schedule",
        ),
        "custody_bank_notices": dict(
            retrieve_custody_notice="obtain_custody_notice",
            allocate_fee_owner=(
                "allocate_fee_to_institution"
                if reference == "INSTITUTION"
                else "allocate_fee_to_customers"
            ),
            settle_fee_allocation=(
                "settle_institution_fee"
                if amount == r.get("additional_fee_minor")
                else "record_fee_receivable_only"
            ),
            certify_coverage="retain_coverage" if amount == 0 else "release_institution_surplus",
        ),
        "channel_business_keys": dict(
            retrieve_instruction_register="obtain_instruction_register",
            bind_business_keys=(
                "bind_original_business_keys"
                if reference == r.get("instruction_batch_reference")
                else "assign_new_business_keys"
            ),
            set_cancelled_instruction_disposition=(
                "quarantine_cancelled_instruction"
                if reference == "CANCELLED"
                else "reactivate_cancelled_instruction"
            ),
            acknowledge_deliveries="acknowledge_unique_delivery",
        ),
        "reservation_commitments": dict(
            retrieve_reservation_contracts="obtain_reservation_contracts",
            set_live_commitment=(
                "protect_live_commitment"
                if amount == r.get("live_commitment_minor")
                else "cancel_live_commitment"
            ),
            bind_supplier_currency="bind_currency_acceptance",
            release_commitment=(
                "release_obsolete_commitment"
                if reference == r.get("obsolete_reference")
                else "release_live_commitment"
            ),
        ),
        "receivable_disputes": dict(
            retrieve_debtor_confirmation="obtain_debtor_confirmation",
            set_collection_tranche=(
                "split_disputed_receivable"
                if amount == r.get("undisputed_minor")
                else "factor_full_receivable"
            ),
            set_disputed_claim_disposition=(
                "preserve_disputed_claim" if reference == "OPEN" else "forgive_disputed_claim"
            ),
            allocate_collection_receipt="reconcile_collected_tranche",
        ),
        "beneficiary_delivery_terms": dict(
            validate_beneficiary="obtain_beneficiary_validation",
            select_beneficiary_account="bind_validated_account",
            obtain_delivery_quote="obtain_delivery_quote",
            set_fee_bearer="choose_sender_fees" if reference == "SENDER" else "choose_shared_fees",
            reconcile_delivery="record_full_delivery",
        ),
        "wallet_purchase_orders": dict(
            retrieve_merchant_order_status="obtain_order_cancellations",
            cancel_order=(
                "cancel_duplicate_order"
                if reference == r.get("cancelled_order_reference")
                else "cancel_required_order"
            ),
            reserve_order_amount="reserve_merchant_purchase",
            set_clearing_priority=(
                "set_settlement_priority" if reference == "MERCHANT" else "sweep_before_merchant"
            ),
            reconcile_capture="reconcile_merchant_capture",
        ),
    }
    return simple[product_kind][action]


DESCRIPTIONS = {
    "delivery_acceptances": "Inspect contracted quantities; a supplier replacement cures a confirmed short delivery. Acceptance binds the original screened supplier and collection account. Warranty release or retention is a separate enforceable supplier promise.",
    "supplier_credit_notes": "Import the supplier credit statement, apply or expressly forgive its credit, authenticate collection-account ownership and bind a remittance reference. Reconciliation preserves the supplier balance and the credit claim.",
    "card_authorizations": "Obtain bank status for authorization holds. A void order can be cancelled; cancelling a delivered order waives its reservation without cancelling its debt. Employee scope and a required-purchase reservation jointly consume available cycle permission.",
    "subscription_consents": "Retrieve pending merchant captures independently of token replacement. Void the unwanted capture, renew named service consents and bind each consent to the current card generation. Cancellation of a token alone does not clear a pending capture.",
    "merchant_refund_cases": "Retrieve merchant refunds and classify their economic component. Evidence of the remaining conversion claim must deduct only matching conversion refunds. A final overlap reconciliation closes the refund liability; ignoring a refund or forgiving the remainder changes customer recovery.",
    "provisional_cash_claims": "Import provisional-credit terms, reconcile bank presentment and allocate the provisional amount against the same withdrawal. Finalization converts only the evidenced entitlement; unmatched provisional money remains repayable.",
    "carrier_fee_claims": "Import parcel-level fee demands, dispute the fee on an unpaid returned parcel, and set off only contracted delivered-parcel commissions. Allocate actual recovered proceeds to buyer invoices; an advance allocation leaves them outstanding.",
    "tax_registrations": "Obtain current destination tax-registration status, register the correct destination, and assign an existing credit only within its jurisdiction. The close must expose the utilized and retained tax-credit schedule.",
    "interaccount_matches": "Obtain the bank transfer reference and pair the debit/credit legs across accounts. Elimination affects consolidated reporting, not either source statement. Publish the consolidated close only after the two complete source days exist.",
    "recovery_period_bindings": "Obtain accounting-period locks; select a current open posting period while preserving the historical lock. A bank-return notice revives the supplier payable. Publish a rollforward from receipt-derived original and reversal components.",
    "correction_components": "Obtain the bank component advice, match the old payment and recognize the component already posted in the current ledger. Publish a component schedule linked to the actual correction and current published report.",
    "custody_bank_notices": "Import a custody fee notice and allocate legal responsibility. Institution settlement pays its own expense; a receivable is not cash. Release only evidenced institution surplus after customer settlement, preserving remaining coverage.",
    "channel_business_keys": "Retrieve each original instruction and stable supplier business key. Bind recovery to those keys, quarantine cancelled originals, and acknowledge one receipt per obligation. Assigning new business keys permits a duplicate that the bank cannot infer is the same commercial debt.",
    "reservation_commitments": "Retrieve contracts behind reservations, preserve the still-live settlement promise, and bind each supplier currency acceptance. Release the obsolete commitment only after both protected currency payments have completed.",
    "receivable_disputes": "Obtain a debtor confirmation separating an undisputed tranche from a disputed claim. Keep the disputed claim alive, collect only presently available money, and reconcile the eventual receipt to its tranche. Factoring the full face or forgiving the dispute changes the commercial claim.",
    "beneficiary_delivery_terms": "Validate beneficiary eligibility independently of route pricing, bind the authenticated account, obtain a quote for the selected delivery terms, and allocate correspondent charges. Sender-paid terms preserve the full principal; shared fees reduce the amount delivered.",
    "wallet_purchase_orders": "Import merchant order cancellations, distinguish a duplicate from the required purchase, reserve the live merchant obligation and set its settlement priority. Reconcile the actual capture to the merchant order after the complete post-configuration observation window.",
}


def register(api):
    operation, row, rows, upd, charge = (
        api.helper(k) for k in ("operation", "row", "rows", "upd", "charge")
    )

    def get(ctx, product_kind):
        records = rows(ctx, PRODUCTS[product_kind][0])
        ctx.require(bool(records), "PRODUCT_CONTRACT_REQUIRED")
        return records[0]

    def update(ctx, product_kind, r, **v):
        return upd(ctx, PRODUCTS[product_kind][0], r["id"], **v)

    def native(ctx, product_kind, record_id, action, reference, amount_minor):
        r = row(ctx, PRODUCTS[product_kind][0], record_id)
        ctx.require(action in PUBLIC_ACTIONS[product_kind], "UNSUPPORTED_BUSINESS_ACTION")
        public_action = action
        action = canonical(product_kind, action, reference, amount_minor, r)
        ctx.require(amount_minor >= 0, "NEGATIVE_AMOUNT")
        source = ctx.record(r["source_collection"], r["source_id"])
        v = {}
        bank = r.get("_bank", {})
        if product_kind == "delivery_acceptances":
            if action == "inspect_goods":
                v = {"received_quantity": bank["received_quantity"], "inspection_received": True}
            elif action == "replace_short_delivery":
                ctx.require(
                    r.get("inspection_received")
                    and r["received_quantity"] < r["contract_quantity"],
                    "NO_CONFIRMED_SHORTAGE",
                )
                v = {
                    "received_quantity": r["contract_quantity"],
                    "replacement_reference": "SUPPLIER-REPLACEMENT-ACCEPTED",
                }
            elif action in ("accept_goods", "waive_missing_goods"):
                ctx.require(r.get("inspection_received"), "GOODS_INSPECTION_REQUIRED")
                v = {
                    "accepted_quantity": r["received_quantity"],
                    "missing_goods_waived": action == "waive_missing_goods",
                }
            elif action in ("qualify_account", "route_to_collection_agent"):
                ctx.require(reference in r["collection_accounts"], "UNKNOWN_COLLECTION_ACCOUNT")
                v = {
                    "collection_account": reference,
                    "account_owner": bank["account_owners"][reference],
                }
            elif action in ("release_warranty", "retain_warranty"):
                ctx.require(source["status"] == "PAID", "SUPPLIER_PAYMENT_REQUIRED")
                v = {
                    "warranty_retained": action == "retain_warranty",
                    "warranty_minor": amount_minor,
                }
        elif product_kind == "supplier_credit_notes":
            if action == "obtain_credit_statement":
                v = {
                    "credit_received": True,
                    "credit_minor": bank["credit_minor"],
                    "credit_invoice": bank["credit_invoice"],
                }
            elif action in ("apply_credit", "forgive_credit"):
                ctx.require(r.get("credit_received"), "CREDIT_STATEMENT_REQUIRED")
                ctx.require(reference == r["credit_invoice"], "CREDIT_INVOICE_MISMATCH")
                ctx.require(amount_minor <= r["credit_minor"], "CREDIT_EXCEEDS_STATEMENT")
                v = {
                    "credit_applied_minor": amount_minor,
                    "credit_forgiven": amount_minor < r["credit_minor"],
                }
                source["amount_minor"] = r["invoice_face_minor"] - amount_minor
            elif action in ("check_collection_account", "accept_email_account"):
                ctx.require(
                    reference in source["bank_valid_accounts"], "UNKNOWN_COLLECTION_ACCOUNT"
                )
                v = {
                    "checked_account": reference,
                    "ownership_confirmed": action == "check_collection_account"
                    and reference == source["_authorized_account"],
                }
            elif action == "bind_invoice_reference":
                ctx.require(r.get("ownership_confirmed"), "ACCOUNT_OWNERSHIP_REQUIRED")
                ctx.require(reference == source["invoice"], "REMITTANCE_INVOICE_MISMATCH")
                v = {"bound_invoice": reference}
            elif action in ("reconcile_supplier_balance", "close_without_credit"):
                ctx.require(source["status"] == "PAID", "PAYMENT_NOT_RECEIVED")
                ctx.require(not r.get("balance_reconciled"), "BALANCE_ALREADY_RECONCILED")
                amt = (
                    r.get("credit_applied_minor", 0)
                    if action == "reconcile_supplier_balance"
                    else 0
                )
                ctx.require(
                    source.get("paid_minor", source["amount_minor"]) + amt
                    == r["invoice_face_minor"],
                    "SUPPLIER_BALANCE_MISMATCH",
                )
                v = {"balance_reconciled": True, "credit_recovered_minor": amt}
        elif product_kind == "card_authorizations":
            if action == "obtain_authorization_status":
                v = {
                    "authorization_received": True,
                    "void_hold_minor": bank["void_hold_minor"],
                    "delivered_hold_minor": bank["delivered_hold_minor"],
                    "void_authorization_reference": bank["void_authorization_reference"],
                    "delivered_authorization_reference": bank["delivered_authorization_reference"],
                }
            elif action in ("cancel_void_authorization", "cancel_delivered_authorization"):
                ctx.require(r.get("authorization_received"), "BANK_AUTHORIZATION_STATUS_REQUIRED")
                v = (
                    {"void_cancelled": True}
                    if action == "cancel_void_authorization"
                    else {"delivered_cancelled": True}
                )
            elif action in ("bind_employee_purchase", "broaden_employee_scope"):
                ctx.require(reference in r["employees"], "EMPLOYEE_NOT_FOUND")
                v = {
                    "employee": reference,
                    "scope": r["merchant"] if action == "bind_employee_purchase" else "*",
                }
            elif action in ("reserve_required_purchase", "release_required_reserve"):
                ctx.require(r.get("authorization_received"), "BANK_AUTHORIZATION_STATUS_REQUIRED")
                v = {"reserved_minor": amount_minor if action == "reserve_required_purchase" else 0}
        elif product_kind == "subscription_consents":
            if action == "obtain_capture_status":
                v = {
                    "capture_received": True,
                    "pending_capture_minor": bank["pending_capture_minor"],
                    "capture_merchant": bank["capture_merchant"],
                }
            elif action in ("void_unwanted_capture", "retain_unwanted_capture"):
                ctx.require(r.get("capture_received"), "CAPTURE_STATUS_REQUIRED")
                v = {"capture_voided": action == "void_unwanted_capture"}
            elif action in ("renew_service_consent", "cancel_service_consent"):
                ctx.require(reference in r["essential_merchants"], "SERVICE_NOT_CONTRACTED")
                consents = dict(r.get("consents", {}))
                consents[reference] = action == "renew_service_consent"
                limits = dict(r.get("consent_limits", {}))
                limits[reference] = amount_minor
                v = {"consents": consents, "consent_limits": limits}
            elif action in ("bind_current_credentials", "permit_any_credentials"):
                card = source
                v = {
                    "consent_generation": (
                        card["generation"] if action == "bind_current_credentials" else 0
                    )
                }
        elif product_kind == "merchant_refund_cases":
            if action == "obtain_refund_statement":
                ctx.require(not r.get("refund_received"), "REFUND_ALREADY_RECEIVED")
                ctx.ledger(
                    source["account_id"],
                    bank["merchant_refund_minor"],
                    "USD",
                    "dcc_adjustment",
                    r["id"],
                )
                v = {
                    "refund_received": True,
                    "merchant_refund_minor": bank["merchant_refund_minor"],
                    "refund_component": bank["refund_component"],
                }
            elif action in ("match_refund_component", "ignore_existing_refund"):
                ctx.require(r.get("refund_received"), "MERCHANT_REFUND_REQUIRED")
                v = {
                    "matched_refund_minor": (
                        amount_minor
                        if action == "match_refund_component" and reference == r["refund_component"]
                        else 0
                    ),
                    "refund_matched": action == "match_refund_component"
                    and reference == r["refund_component"],
                }
            elif action in ("submit_remaining_evidence", "accept_merchant_denial"):
                ctx.require(source.get("audit_received"), "TERMINAL_EVIDENCE_REQUIRED")
                v = {
                    "evidence_submitted": action == "submit_remaining_evidence",
                    "claim_remaining_minor": amount_minor,
                    "merchant_denial_accepted": action == "accept_merchant_denial",
                }
            elif action in ("settle_refund_overlap", "forgive_remaining_claim"):
                ctx.require(source.get("audit_received"), "TERMINAL_EVIDENCE_REQUIRED")
                v = {
                    "refund_liability_closed": True,
                    "remaining_forgiven": action == "forgive_remaining_claim",
                    "final_component": reference,
                }
        elif product_kind == "provisional_cash_claims":
            if action == "obtain_provisional_credit":
                ctx.require(not r.get("provisional_received"), "PROVISIONAL_ALREADY_RECEIVED")
                ctx.ledger(
                    source["account_id"],
                    bank["provisional_minor"],
                    "USD",
                    "cash_shortfall_credit",
                    r["id"],
                )
                v = {
                    "provisional_received": True,
                    "provisional_minor": bank["provisional_minor"],
                    "repayable": True,
                }
            elif action == "reconcile_presentment":
                ctx.require(source.get("log_received"), "DEVICE_PRESENTMENT_REQUIRED")
                v = {
                    "evidenced_delivery_minor": source["presented_minor"]
                    - source["retracted_minor"]
                }
            elif action in ("allocate_provisional_credit", "keep_provisional_unmatched"):
                ctx.require(r.get("provisional_received"), "PROVISIONAL_TERMS_REQUIRED")
                v = {
                    "allocated_minor": (
                        amount_minor if action == "allocate_provisional_credit" else 0
                    ),
                    "withdrawal_reference": reference,
                }
            elif action in ("finalize_provisional_credit", "return_entire_provisional"):
                ctx.require(source["status"] == "CLOSED", "FINAL_WITHDRAWAL_DISPOSITION_REQUIRED")
                v = {
                    "repayable": False,
                    "provisional_finalized": action == "finalize_provisional_credit",
                    "returned_minor": (
                        r["provisional_minor"] if action == "return_entire_provisional" else 0
                    ),
                }
                if action == "return_entire_provisional":
                    ctx.ledger(
                        source["account_id"],
                        -r["provisional_minor"],
                        "USD",
                        "provisional_credit_return",
                        r["id"],
                    )
        elif product_kind == "carrier_fee_claims":
            if action == "obtain_carrier_fee_statement":
                v = {
                    "fee_statement_received": True,
                    "returned_parcel_fee_minor": bank["returned_parcel_fee_minor"],
                    "fee_parcel": bank["fee_parcel"],
                }
            elif action in ("contest_return_fee", "accept_return_fee"):
                ctx.require(r.get("fee_statement_received"), "CARRIER_FEE_STATEMENT_REQUIRED")
                v = {
                    "returned_fee_disputed": amount_minor == 0,
                    "accepted_extra_fee_minor": amount_minor,
                }
                if amount_minor:
                    ctx.ledger(
                        source["account_id"], -amount_minor, "USD", "carrier_extra_fee", r["id"]
                    )
            elif action == "offset_contract_commission":
                ctx.require(source.get("inventoried"), "INVENTORY_REQUIRED")
                v = {"commission_offset_minor": amount_minor}
            elif action == "waive_carrier_receivable":
                v = {"carrier_claim_waived": True}
            elif action in ("allocate_parcel_remittance", "allocate_as_advance"):
                ctx.require(source.get("cash_remitted"), "CARRIER_REMITTANCE_REQUIRED")
                v = {
                    "allocation_kind": (
                        "buyer_invoices" if action == "allocate_parcel_remittance" else "advance"
                    ),
                    "allocated_minor": amount_minor,
                }
        elif product_kind == "tax_registrations":
            if action == "obtain_jurisdiction_status":
                v = {
                    "registry_received": True,
                    "registered_jurisdictions": bank["registered_jurisdictions"],
                    "credit_jurisdiction": bank["credit_jurisdiction"],
                    "credit_minor": bank["credit_minor"],
                }
            elif action in ("register_destination", "register_origin"):
                ctx.require(source["events_current"], "CURRENT_TAX_EVENTS_REQUIRED")
                v = {
                    "registration": (
                        r["destination"] if action == "register_destination" else r["origin"]
                    )
                }
            elif action in ("apply_same_jurisdiction_credit", "apply_other_jurisdiction_credit"):
                ctx.require(r.get("registry_received"), "TAX_REGISTRY_REQUIRED")
                v = {
                    "credit_assigned_jurisdiction": (
                        r["credit_jurisdiction"]
                        if action == "apply_same_jurisdiction_credit"
                        else r["other_jurisdiction"]
                    ),
                    "credit_reserved_minor": amount_minor,
                }
            elif action in ("publish_tax_credit_schedule", "forfeit_credit"):
                ctx.require(source["status"] == "CLOSED", "TAX_CLOSE_REQUIRED")
                v = {
                    "credit_schedule_published": True,
                    "credit_preserved_minor": (
                        r["credit_minor"] if action == "publish_tax_credit_schedule" else 0
                    ),
                }
        elif product_kind == "interaccount_matches":
            if action == "obtain_transfer_reference":
                v = {
                    "advice_received": True,
                    "bank_transfer_reference": bank["bank_transfer_reference"],
                    "debit_minor": bank["debit_minor"],
                    "credit_minor": bank["credit_minor"],
                }
            elif action in ("match_transfer_pair", "match_same_sign_lines"):
                ctx.require(r.get("advice_received"), "TRANSFER_ADVICE_REQUIRED")
                v = {
                    "matched_reference": reference,
                    "pair_direction": "opposite" if action == "match_transfer_pair" else "same",
                }
            elif action in ("post_interaccount_elimination", "classify_as_sales"):
                ctx.require(r.get("matched_reference"), "TRANSFER_PAIR_REQUIRED")
                v = {
                    "eliminated_minor": (
                        amount_minor if action == "post_interaccount_elimination" else 0
                    ),
                    "consolidated_category": reference,
                }
                api.helper("add")(
                    ctx,
                    "consolidation_postings",
                    contract_id=r["id"],
                    bank_transfer_reference=r["matched_reference"],
                    amount_minor=amount_minor,
                    category=reference,
                )
            elif action in ("publish_consolidated_close", "discard_transfer_receipt"):
                ctx.require(
                    all(
                        (
                            ctx.record("xp_c_statements", s)["status"] == "CLOSED"
                            for s in r["statement_ids"]
                        )
                    ),
                    "BOTH_BANK_DAYS_REQUIRED",
                )
                entries = rows(ctx, "statement_entries")
                income = sum(
                    (
                        e["amount_minor"]
                        for e in entries
                        if e["amount_minor"] > 0 and e.get("category") == "sales"
                    )
                )
                income += sum(
                    (
                        p["amount_minor"]
                        for p in rows(ctx, "consolidation_postings", contract_id=r["id"])
                        if p["category"] == "sales"
                    )
                )
                v = {
                    "consolidated_published": True,
                    "advice_retained": action == "publish_consolidated_close",
                    "external_income_minor": income,
                }
        elif product_kind == "recovery_period_bindings":
            if action == "obtain_period_locks":
                v = {
                    "locks_received": True,
                    "historical_locked": bank["historical_locked"],
                    "open_period": bank["open_period"],
                }
            elif action in ("bind_open_period", "reopen_historical_period"):
                ctx.require(r.get("locks_received"), "PERIOD_LOCKS_REQUIRED")
                v = {
                    "posting_period": (
                        r["open_period"] if action == "bind_open_period" else r["historical_period"]
                    ),
                    "historical_preserved": action == "bind_open_period",
                }
            elif action in ("recognize_returned_payable", "write_off_returned_payable"):
                ex = ctx.record("xp_c_executions", r["return_execution"])
                ctx.require(ex["bank_status"] == "RETURNED", "FINAL_RETURN_RECEIPT_REQUIRED")
                v = {
                    "restored_payable_minor": (
                        amount_minor if action == "recognize_returned_payable" else 0
                    ),
                    "return_reference": reference,
                }
                existing = rows(ctx, "restored_supplier_payables", execution_id=ex["id"])
                if existing:
                    upd(
                        ctx,
                        "restored_supplier_payables",
                        existing[0]["id"],
                        open_minor=v["restored_payable_minor"],
                        return_reference=reference,
                        accounting_period=r["posting_period"],
                    )
                else:
                    api.helper("add")(
                        ctx,
                        "restored_supplier_payables",
                        execution_id=ex["id"],
                        open_minor=v["restored_payable_minor"],
                        return_reference=reference,
                        accounting_period=r["posting_period"],
                    )
            elif action in ("publish_recovery_rollforward", "discard_rollforward"):
                ctx.require(
                    all(
                        (
                            ctx.record("xp_c_recovery_orders", s)["status"] in ("PAID", "RETURNED")
                            for s in r["order_ids"]
                        )
                    ),
                    "RECOVERY_PROJECTIONS_REQUIRED",
                )
                v = {"rollforward_published": action == "publish_recovery_rollforward"}
        elif product_kind == "correction_components":
            if action == "obtain_component_advice":
                v = {
                    "component_advice_received": True,
                    "existing_component_minor": bank["existing_component_minor"],
                    "existing_component_reference": bank["existing_component_reference"],
                }
            elif action in ("match_prior_payment", "match_other_payment"):
                ctx.require(r.get("component_advice_received"), "BANK_COMPONENT_ADVICE_REQUIRED")
                v = {"matched_payment": reference}
            elif action in ("recognize_existing_component", "duplicate_existing_component"):
                ctx.require(source.get("correction_received"), "ACTUAL_BANK_CORRECTION_REQUIRED")
                v = {
                    "existing_component_count": (
                        1 if action == "recognize_existing_component" else 2
                    ),
                    "component_minor": amount_minor,
                }
            elif action in ("publish_component_schedule", "net_without_lineage"):
                ctx.require(
                    source["current_status"] == "CLOSED", "CURRENT_REPORT_PUBLICATION_REQUIRED"
                )
                v = {
                    "schedule_published": True,
                    "lineage_retained": action == "publish_component_schedule",
                    "report_component_reference": reference,
                }
        elif product_kind == "custody_bank_notices":
            if action == "obtain_custody_notice":
                ctx.require(not r.get("notice_received"), "CUSTODY_NOTICE_ALREADY_IMPORTED")
                ctx.ledger(
                    source["account_id"],
                    -bank["additional_fee_minor"],
                    "USD",
                    "late_custody_bank_fee",
                    r["id"],
                )
                v = {
                    "notice_received": True,
                    "additional_fee_minor": bank["additional_fee_minor"],
                    "legal_owner": bank["legal_owner"],
                }
            elif action in ("allocate_fee_to_institution", "allocate_fee_to_customers"):
                ctx.require(r.get("notice_received"), "CUSTODY_NOTICE_REQUIRED")
                v = {
                    "fee_owner": (
                        "INSTITUTION" if action == "allocate_fee_to_institution" else "CUSTOMERS"
                    )
                }
                if action == "allocate_fee_to_customers":
                    liab = ctx.record(
                        "xp_c_customer_liabilities", source["settlement_liability_id"]
                    )
                    liab["liability_minor"] -= r["additional_fee_minor"]
                    liab["customer_fee_minor"] = (
                        liab.get("customer_fee_minor", 0) + r["additional_fee_minor"]
                    )
            elif action in ("settle_institution_fee", "record_fee_receivable_only"):
                ctx.require(r.get("notice_received"), "CUSTODY_NOTICE_REQUIRED")
                ctx.require(not r.get("fee_disposed"), "FEE_ALREADY_DISPOSED")
                if action == "settle_institution_fee":
                    own = -sum(
                        (
                            j["amount_minor"]
                            for j in ctx.records("xp_journal")
                            if j.get("record_type") == "cash_posting"
                            and j.get("account_id") == source["operating_account_id"]
                            and (j.get("kind") == "institutional_topup")
                        )
                    )
                    ctx.require(
                        own >= source["bank_fee_minor"] + r["additional_fee_minor"],
                        "REAL_INSTITUTION_REPLACEMENT_REQUIRED",
                    )
                v = {"fee_disposed": True, "fee_paid": action == "settle_institution_fee"}
            elif action in ("release_institution_surplus", "retain_coverage"):
                remain = sum(
                    (
                        x["liability_minor"]
                        for x in rows(ctx, "customer_liabilities", pool_id=source["id"])
                    )
                )
                bal = ctx.record("xp_accounts", source["account_id"])["balance_minor"]
                if action == "release_institution_surplus":
                    ctx.ledger(
                        source["account_id"],
                        -amount_minor,
                        "USD",
                        "custody_surplus_release",
                        r["id"],
                    )
                    ctx.ledger(
                        source["operating_account_id"],
                        amount_minor,
                        "USD",
                        "custody_surplus_received",
                        r["id"],
                    )
                v = {
                    "coverage_certified": True,
                    "certified_liabilities_minor": remain,
                    "certified_cash_minor": bal
                    - (amount_minor if action == "release_institution_surplus" else 0),
                }
        elif product_kind == "channel_business_keys":
            if action == "obtain_instruction_register":
                v = {
                    "register_received": True,
                    "original_business_keys": bank["original_business_keys"],
                }
            elif action in ("bind_original_business_keys", "assign_new_business_keys"):
                ctx.require(r.get("register_received"), "INSTRUCTION_REGISTER_REQUIRED")
                v = {
                    "bound_business_keys": (
                        r["original_business_keys"]
                        if action == "bind_original_business_keys"
                        else {k: "NEW-" + v for (k, v) in r["original_business_keys"].items()}
                    )
                }
            elif action in ("quarantine_cancelled_instruction", "reactivate_cancelled_instruction"):
                v = {"cancelled_quarantined": action == "quarantine_cancelled_instruction"}
            elif action in ("acknowledge_unique_delivery", "acknowledge_duplicate_delivery"):
                items = rows(ctx, "channel_items", batch_id=source["id"])
                ctx.require(
                    all(
                        (
                            any((d["item_id"] == x["id"] for d in rows(ctx, "channel_deliveries")))
                            for x in items
                        )
                    ),
                    "ALL_SUPPLIER_DELIVERIES_REQUIRED",
                )
                v = {
                    "acknowledged_keys": list(r["bound_business_keys"].values()),
                    "duplicate_acknowledged": action == "acknowledge_duplicate_delivery",
                }
                for item in items:
                    if r["bound_business_keys"].get(item["id"]) == r["original_business_keys"].get(
                        item["id"]
                    ):
                        item["business_status"] = "PAID"
                    else:
                        item["business_status"] = "UNALLOCATED_RECEIPT"
        elif product_kind == "reservation_commitments":
            if action == "obtain_reservation_contracts":
                v = {
                    "contracts_received": True,
                    "live_commitment_minor": bank["live_commitment_minor"],
                    "obsolete_reference": bank["obsolete_reference"],
                }
            elif action in ("protect_live_commitment", "cancel_live_commitment"):
                ctx.require(r.get("contracts_received"), "RESERVATION_CONTRACTS_REQUIRED")
                v = {"live_protected": action == "protect_live_commitment"}
                escrow = ctx.record("xp_accounts", r["commitment_account_id"])
                delta = amount_minor - escrow["balance_minor"]
                if delta:
                    ctx.ledger(
                        source["account_id"],
                        -delta,
                        "USD",
                        "commitment_collateral_transfer",
                        r["id"],
                    )
                    ctx.ledger(escrow["id"], delta, "USD", "commitment_collateral_held", r["id"])
            elif action in ("bind_currency_acceptance", "force_currency_substitution"):
                ctx.require(reference in r["supplier_currencies"], "SUPPLIER_NOT_FOUND")
                v = {
                    "currency_acceptance": dict(
                        r.get("currency_acceptance", {}),
                        **{
                            reference: (
                                r["supplier_currencies"][reference]
                                if action == "bind_currency_acceptance"
                                else "USD"
                            )
                        },
                    )
                }
            elif action in ("release_obsolete_commitment", "release_live_commitment"):
                ctx.require(
                    all(
                        (
                            x["status"] == "PAID"
                            for x in rows(ctx, "resource_payments", queue_id=source["id"])
                        )
                    ),
                    "PROTECTED_PAYMENTS_REQUIRED",
                )
                v = {
                    "obsolete_released": action == "release_obsolete_commitment",
                    "live_protected": action == "release_obsolete_commitment"
                    and r.get("live_protected", False),
                }
                if action == "release_live_commitment":
                    escrow = ctx.record("xp_accounts", r["commitment_account_id"])
                    amount = escrow["balance_minor"]
                    ctx.ledger(
                        escrow["id"], -amount, "USD", "commitment_collateral_released", r["id"]
                    )
                    ctx.ledger(
                        source["account_id"],
                        amount,
                        "USD",
                        "commitment_collateral_received",
                        r["id"],
                    )
        elif product_kind == "receivable_disputes":
            if action == "obtain_debtor_confirmation":
                v = {
                    "debtor_confirmed": True,
                    "undisputed_minor": bank["undisputed_minor"],
                    "disputed_minor": bank["disputed_minor"],
                    "debt_reference": bank["debt_reference"],
                }
            elif action in ("split_disputed_receivable", "factor_full_receivable"):
                ctx.require(r.get("debtor_confirmed"), "DEBTOR_CONFIRMATION_REQUIRED")
                v = {
                    "collectible_minor": amount_minor,
                    "recourse_assumed": amount_minor > r["undisputed_minor"],
                }
                source["receivable_minor"] = amount_minor
            elif action in ("preserve_disputed_claim", "forgive_disputed_claim"):
                if (
                    action == "preserve_disputed_claim"
                    and r.get("claim_waiver_irreversible")
                    and (r.get("claim_preserved") is False)
                ):
                    ctx.require(False, "DISPUTED_CLAIM_ALREADY_WAIVED")
                v = {"claim_preserved": action == "preserve_disputed_claim"}
            elif action in ("reconcile_collected_tranche", "close_uncollected_claim"):
                ctx.require(source["receivable_collected"], "RECEIVABLE_COLLECTION_REQUIRED")
                v = {
                    "receipt_allocated_minor": amount_minor,
                    "claim_closed": action == "close_uncollected_claim",
                    "allocated_reference": reference,
                }
        elif product_kind == "beneficiary_delivery_terms":
            if action == "obtain_beneficiary_validation":
                v = {
                    "validation_received": True,
                    "validated_account": bank["validated_account"],
                    "beneficiary_status": bank["beneficiary_status"],
                }
            elif action in ("bind_validated_account", "bind_unverified_account"):
                ctx.require(r.get("validation_received"), "BENEFICIARY_VALIDATION_REQUIRED")
                v = {"bound_account": reference}
            elif action == "obtain_delivery_quote":
                ctx.require(r.get("bound_account"), "BENEFICIARY_BINDING_REQUIRED")
                v = {
                    "quote_received": True,
                    "quote_account": r["bound_account"],
                    "correspondent_fee_minor": max(
                        0,
                        bank["correspondent_fee_minor"]
                        - (100 if reference == "direct_member" else 0),
                    ),
                    "quote_expires_minute": ctx.minute + r["quote_lifetime_minutes"],
                }
            elif action in ("choose_sender_fees", "choose_shared_fees"):
                ctx.require(r.get("quote_received"), "DELIVERY_QUOTE_REQUIRED")
                v = {"fee_bearer": "SENDER" if action == "choose_sender_fees" else "SHARED"}
            elif action in ("record_full_delivery", "accept_short_delivery"):
                ctx.require(source["status"] == "PAID", "ROUTE_PAYMENT_REQUIRED")
                v = {
                    "delivery_confirmed": True,
                    "received_principal_minor": (
                        source["principal_minor"]
                        if action == "record_full_delivery" and r.get("fee_bearer") == "SENDER"
                        else source["principal_minor"] - r["correspondent_fee_minor"]
                    ),
                }
        elif action == "obtain_order_cancellations":
            v = {
                "order_status_received": True,
                "cancelled_order_reference": bank["cancelled_order_reference"],
                "required_order_reference": bank["required_order_reference"],
            }
        elif action in ("cancel_duplicate_order", "cancel_required_order"):
            ctx.require(r.get("order_status_received"), "MERCHANT_ORDER_STATUS_REQUIRED")
            v = {
                "cancelled_reference": (
                    r["cancelled_order_reference"]
                    if action == "cancel_duplicate_order"
                    else r["required_order_reference"]
                )
            }
        elif action in ("reserve_merchant_purchase", "release_merchant_reserve"):
            v = {
                "merchant_reserved_minor": (
                    amount_minor if action == "reserve_merchant_purchase" else 0
                )
            }
        elif action in ("set_settlement_priority", "sweep_before_merchant"):
            v = {
                "settlement_priority": (
                    "MERCHANT" if action == "set_settlement_priority" else "SWEEP"
                )
            }
        elif action == "reconcile_merchant_capture":
            ctx.require(source["purchase_paid"], "MERCHANT_CAPTURE_REQUIRED")
            v = {"capture_reference": reference, "captured_minor": amount_minor}
        ctx.require(bool(v), "ACTION_HAS_NO_BUSINESS_EFFECT")
        result = update(ctx, product_kind, r, **v)
        ctx.emit(
            PRODUCTS[product_kind][0] + "_commitment",
            record_id=r["id"],
            action=public_action,
            reference=reference,
            amount_minor=amount_minor,
        )
        return result

    for product_kind, (_, name, _) in PRODUCTS.items():

        def fn(ctx, record_id, action, reference, amount_minor, _product_kind=product_kind):
            return native(ctx, _product_kind, record_id, action, reference, amount_minor)

        operation(
            name,
            DESCRIPTIONS[product_kind]
            + " Actions: "
            + ", ".join(PUBLIC_ACTIONS[product_kind])
            + ". reference identifies the public business reference or named party; amount_minor records the explicit amount involved. Read the product article for local dependencies.",
            {
                "record_id": "string",
                "action": PUBLIC_ACTIONS[product_kind],
                "reference": "string",
                "amount_minor": "integer",
            },
        )(fn)

    for product_kind, (_, _, name) in PRODUCTS.items():
        if name not in api.operations:
            continue
        original = api.operations[name]["function"]

        def wrapped(ctx, _product_kind=product_kind, _original=original, **args):
            linked = rows(ctx, PRODUCTS[_product_kind][0])
            if linked:
                r = linked[0]
                required = {
                    "delivery_acceptances": "accepted_quantity",
                    "supplier_credit_notes": "bound_invoice",
                    "card_authorizations": "employee",
                    "subscription_consents": "capture_received",
                    "merchant_refund_cases": "refund_received",
                    "provisional_cash_claims": "provisional_received",
                    "carrier_fee_claims": "fee_statement_received",
                    "tax_registrations": "registry_received",
                    "interaccount_matches": "matched_reference",
                    "recovery_period_bindings": "posting_period",
                    "correction_components": "component_advice_received",
                    "custody_bank_notices": "fee_owner",
                    "channel_business_keys": "bound_business_keys",
                    "reservation_commitments": "currency_acceptance",
                    "receivable_disputes": "collectible_minor",
                    "beneficiary_delivery_terms": "fee_bearer",
                    "wallet_purchase_orders": "settlement_priority",
                }[_product_kind]
                ctx.require(required in r, "LINKED_" + required.upper() + "_REQUIRED")
                if _product_kind == "card_authorizations":
                    card = ctx.record(r["source_collection"], r["source_id"])
                    p = ctx.record("xp_c_card_purchases", args["purchase_id"])
                    remaining_hold = r.get(
                        "delivered_hold_minor", r["_bank"]["delivered_hold_minor"]
                    ) * (not r.get("delivered_cancelled")) + r.get(
                        "void_hold_minor", r["_bank"]["void_hold_minor"]
                    ) * (
                        not r.get("void_cancelled")
                    )
                    ctx.require(
                        card["spent_minor"] + remaining_hold + p["amount_minor"]
                        <= card["cycle_cap_minor"],
                        "OPEN_AUTHORIZATION_CONSUMES_CYCLE",
                    )
                if _product_kind == "delivery_acceptances":
                    source = ctx.record(r["source_collection"], r["source_id"])
                    if args["hit_id"] == source["id"]:
                        source["amount_minor"] = r["accepted_quantity"] * r["unit_price_minor"]
                        source["paid_collection_account"] = r.get("collection_account")
                if _product_kind == "supplier_credit_notes":
                    source = ctx.record(r["source_collection"], r["source_id"])
                    ctx.require(
                        args["account_reference"] == r.get("checked_account"),
                        "COLLECTION_BINDING_CHANGED",
                    )
                if _product_kind == "subscription_consents":
                    tokens = rows(ctx, "network_tokens", card_id=r["source_id"])
                    for token in tokens:
                        if token["merchant"] in r["essential_merchants"] and (
                            not r.get("consents", {}).get(token["merchant"])
                            or r.get("consent_limits", {}).get(token["merchant"], 0)
                            < token["amount_minor"]
                        ):
                            token["service_active"] = False
                            token["status"] = "STALE"
                    if r.get("pending_capture_minor") and (not r.get("capture_voided")):
                        card = ctx.record(r["source_collection"], r["source_id"])
                        ctx.ledger(
                            card["account_id"],
                            -r["pending_capture_minor"],
                            "USD",
                            "old_subscription_capture",
                            r["id"],
                        )
                        for token in tokens:
                            if token["merchant"] == r["capture_merchant"]:
                                token["billed"] = True
                if (
                    _product_kind == "merchant_refund_cases"
                    and args["transaction_id"] == r["source_id"]
                    and (args["resolution"] == "formal_adjustment")
                ):
                    t = ctx.record(r["source_collection"], r["source_id"])
                    ctx.require(
                        t["status"] == "OPEN" and t.get("consent_choice") == "LOCAL",
                        "NO_OPEN_LOCAL_CONVERSION_CLAIM",
                    )
                    total = (
                        t["billed_minor"]
                        - t["local_principal_minor"]
                        * t["network_rate_numerator"]
                        // t["network_rate_denominator"]
                        - t["legitimate_fee_minor"]
                    )
                    remaining = total - r.get("matched_refund_minor", 0)
                    ctx.require(
                        r.get("claim_remaining_minor") == remaining,
                        "REMAINING_CONVERSION_EVIDENCE_MISMATCH",
                    )
                    charge(ctx, t, t["service_fees_minor"][args["service"]], "dcc_dispute_fee")
                    ctx.ledger(t["account_id"], remaining, "USD", "dcc_adjustment", t["id"])
                    return upd(
                        ctx,
                        "dcc_transactions",
                        t["id"],
                        status="CLOSED",
                        credited_minor=r["merchant_refund_minor"] + remaining,
                        resolution="formal_adjustment",
                    )
                if _product_kind == "provisional_cash_claims" and args["cash_transaction_id"] == r["source_id"]:
                    t = ctx.record(r["source_collection"], r["source_id"])
                    ctx.require(t["status"] == "OPEN", "CASH_CLAIM_CLOSED")
                    ctx.require(
                        args["choice"] != "exact" or t.get("log_received"), "DELIVERY_LOG_REQUIRED"
                    )
                    total = t["debited_minor"] - (
                        t.get("presented_minor", 0) - t.get("retracted_minor", 0)
                    )
                    ctx.require(
                        args["choice"] != "exact" or args["amount_minor"] == total,
                        "CLAIM_AMOUNT_UNSUPPORTED",
                    )
                    net = args["amount_minor"] - r.get("allocated_minor", 0)
                    ctx.require(
                        args["choice"] != "full_and_final"
                        or args["amount_minor"] == t["settlement_offer_minor"],
                        "OFFER_AMOUNT_MISMATCH",
                    )
                    ctx.require(
                        args["choice"] != "waive" or args["amount_minor"] == 0,
                        "WAIVER_HAS_NO_CREDIT",
                    )
                    ctx.ledger(t["account_id"], net, "USD", "cash_shortfall_credit", t["id"])
                    return upd(
                        ctx,
                        "cash_transactions",
                        t["id"],
                        status="CLOSED",
                        credited_minor=r["provisional_minor"] + net,
                        settlement_choice=args["choice"],
                    )
                if _product_kind == "carrier_fee_claims" and args.get("action") == "remit_cash":
                    expected = sum(
                        (
                            o["commission_minor"]
                            for o in rows(ctx, "cod_orders", batch_id=args["batch_id"])
                            if o["delivery_status"] == "DELIVERED"
                        )
                    )
                    ctx.require(
                        r.get("commission_offset_minor") == expected,
                        "CONTRACT_COMMISSION_OFFSET_REQUIRED",
                    )
                if _product_kind == "tax_registrations":
                    orders = [ctx.record("xp_c_tax_orders", i) for i in args["order_ids"]]
                    ctx.require(r.get("registration"), "DESTINATION_REGISTRATION_REQUIRED")
                    batch = ctx.record("xp_c_tax_batches", args["tax_batch_id"])
                    result = []
                    remaining = max(0, r.get("credit_reserved_minor", 0) - r.get("credit_used", 0))
                    for order in orders:
                        ctx.require(order["batch_id"] == batch["id"], "WRONG_BATCH")
                        liability = order["tax_minor"] - order["refunded_tax_minor"]
                        used = min(liability, remaining)
                        remaining -= used
                        charge(ctx, batch, liability - used, "sales_tax_remittance")
                        result.append(
                            api.helper("add")(
                                ctx,
                                "tax_remittances",
                                batch_id=batch["id"],
                                order_id=order["id"],
                                amount_minor=liability,
                                cash_amount_minor=liability - used,
                                credit_minor=used,
                                credit_jurisdiction=r.get("credit_assigned_jurisdiction"),
                                collector_at_submission=order["collector"],
                            )
                        )
                        r["credit_used"] = r.get("credit_used", 0) + used
                    return {"remittances": result}
                if _product_kind == "recovery_period_bindings":
                    existing = {p["id"] for p in rows(ctx, "postings")}
                    result = _original(ctx, **args)
                    for posting in rows(ctx, "postings"):
                        if posting["id"] not in existing:
                            posting["accounting_period"] = r["posting_period"]
                    return result
                if _product_kind == "correction_components":
                    ctx.require(r.get("matched_payment"), "PRIOR_PAYMENT_MATCH_REQUIRED")
                    extra = (r.get("existing_component_count", 0) - 1) * r.get(
                        "existing_component_minor", 0
                    )
                    if extra:
                        ctx.ledger(
                            ctx.record(r["source_collection"], r["source_id"])["account_id"],
                            -extra,
                            "USD",
                            "duplicated_bank_component",
                            r["id"],
                        )
                    result = _original(ctx, **args)
                    result["prior_payment_reference"] = r["matched_payment"]
                    return result
                if _product_kind == "channel_business_keys":
                    result = _original(ctx, **args)
                    if result.get("item_id"):
                        result["business_key"] = r["bound_business_keys"].get(args["item_id"])
                    return result
                if _product_kind == "reservation_commitments":
                    payment = ctx.record("xp_c_resource_payments", args["payment_id"])
                    ctx.require(
                        r["currency_acceptance"].get(payment["id"]) == payment["currency"],
                        "SUPPLIER_CURRENCY_ACCEPTANCE_REQUIRED",
                    )
                if _product_kind == "receivable_disputes":
                    ctx.require(
                        r.get("claim_preserved") is not None, "DISPUTED_CLAIM_DISPOSITION_REQUIRED"
                    )
                if _product_kind == "beneficiary_delivery_terms":
                    ctx.require(ctx.minute <= r["quote_expires_minute"], "DELIVERY_QUOTE_EXPIRED")
                    ctx.require(
                        r["bound_account"] == r["quote_account"], "QUOTE_BENEFICIARY_CHANGED"
                    )
                    offer = ctx.record(r["source_collection"], r["source_id"])
                    if r["fee_bearer"] == "SENDER":
                        ctx.ledger(
                            offer["account_id"],
                            -r["correspondent_fee_minor"],
                            "USD",
                            "correspondent_delivery_fee",
                            r["id"],
                        )
                    result = _original(ctx, **args)
                    result["beneficiary_received_minor"] = offer["principal_minor"] - (
                        r["correspondent_fee_minor"] if r["fee_bearer"] == "SHARED" else 0
                    )
                    result["beneficiary_account"] = r["bound_account"]
                    return result
                if _product_kind == "wallet_purchase_orders":
                    ctrl = ctx.record(r["source_collection"], r["source_id"])
                    ctx.require(
                        r.get("merchant_reserved_minor", 0) >= ctrl["purchase_minor"],
                        "MERCHANT_PURCHASE_RESERVATION_REQUIRED",
                    )
                    if r.get("cancelled_reference") == r.get("required_order_reference"):
                        ctx.require(False, "MERCHANT_ORDER_CANCELLED")
                    if r.get("settlement_priority") == "SWEEP":
                        ctrl["purchase_due_cycle"] += args["cycles"]
            return _original(ctx, **args)

        api.operations[name]["function"] = wrapped
