"""New explicit synthetic execution terms; original SOP facts remain separate."""

from payment.runtime.products.banking.specification import CAPABILITIES as CORE_CAPABILITIES

P = dict(
    business_day_minutes=480,
    contact_review=960,
    verification_valid=60,
    quote_valid=20,
    payment_delay=30,
    authorization_backfill=480,
    registration_review=480,
    recipient_review=30,
    group_review=240,
    unbind_delay=2880,
    fee_share_review=480,
    fee_application_minor=250,
    bank_trace_delay=30,
    return_delay=480,
    return_fee_minor=300,
    void_delay=30,
    card_settle_delay=10,
    cny_rate=7,
    cny_fee_minor=2000,
)
TABLES = (
    "portal_accounts",
    "portal_stores",
    "portal_orders",
    "portal_recipients",
    "portal_payments",
    "portal_security",
    "portal_challenges",
    "portal_groups",
    "portal_members",
    "portal_children",
    "portal_volume",
    "portal_agreements",
    "portal_applications",
    "portal_incoming",
    "portal_traces",
    "portal_refunds",
    "portal_cards",
    "portal_holds",
    "portal_invoices",
)


def cap(topic, description, **parameters):
    return dict(topic=topic, description=description, parameters=parameters)


NEW_CAPABILITIES = {
    "propose_portal_authorization": cap(
        "portal_authorization",
        "Authorize or cancel one seller connection for a specific Portal account.",
        store_id="string",
        account_id="string",
        action="string",
    ),
    "propose_portal_sales_import": cap(
        "portal_authorization",
        "Import assessed historical sales evidence for one authorized store; duplicates do not add quota.",
        store_id="string",
        submission_id="string",
    ),
    "inspect_portal_payment": cap(
        "portal_payments",
        "Quote a Portal bank payment or USD-to-CNY withdrawal using current tariff, bank and security versions.",
        account_id="string",
        recipient_id="string",
        amount_minor="integer",
        currency="string",
        reference="string",
    ),
    "propose_portal_payment": cap(
        "portal_payments",
        "Submit a quoted Portal payment; settlement is a later event.",
        quote_id="string",
    ),
    "propose_portal_contact_change": cap(
        "portal_security",
        "Submit account-recovery evidence for a phone change. Review does not immediately restore SMS.",
        account_id="string",
        submission_id="string",
    ),
    "propose_portal_verification": cap(
        "portal_security",
        "Issue a customer verification challenge on SMS or EMAIL; channels have different business uses.",
        account_id="string",
        channel="string",
    ),
    "propose_rmb_registration": cap(
        "portal_rmb",
        "Register the declared reporting party for an HK company CNY service.",
        account_id="string",
        reporting_party="string",
        submission_id="string",
    ),
    "propose_portal_recipient_verification": cap(
        "portal_recipients",
        "Submit current recipient identity and bank evidence for asynchronous verification.",
        recipient_id="string",
        submission_id="string",
    ),
    "propose_group_invitation": cap(
        "portal_groups",
        "Invite a controlled child account to a parent group; pending invitations occupy places.",
        group_id="string",
        child_id="string",
    ),
    "propose_group_unbinding": cap(
        "portal_groups",
        "Cancel a pending invitation or request a delayed unbinding of an active child.",
        member_id="string",
    ),
    "propose_group_expansion": cap(
        "portal_groups",
        "Request five additional places; qualifying volume and active membership are checked at review.",
        group_id="string",
    ),
    "propose_portal_bank_trace": cap(
        "portal_returns",
        "Request independent origin advice for one closed-account receipt.",
        incoming_id="string",
    ),
    "propose_portal_return": cap(
        "portal_returns",
        "Return a specified amount of a held receipt through ORIGINAL or ALTERNATIVE routing.",
        incoming_id="string",
        amount_minor="integer",
        route="string",
        recipient_id="string",
        submission_id="string",
    ),
    "propose_portal_card_void": cap(
        "portal_cards",
        "Send a merchant void request for an authorization; the merchant determines capture and release.",
        hold_id="string",
    ),
    "propose_portal_card_funding": cap(
        "portal_cards",
        "Set the account for new card purchases; existing holds remain with their original account.",
        card_id="string",
        account_id="string",
    ),
    "propose_portal_card_purchase": cap(
        "portal_cards",
        "Submit a new card purchase for a specific invoice using the card funding account.",
        card_id="string",
        invoice_id="string",
        amount_minor="integer",
    ),
    "propose_portal_fee_share": cap(
        "portal_fees",
        "Request a relationship-based tariff; an application fee is charged even if review rejects it.",
        account_id="string",
        agreement_id="string",
        submission_id="string",
    ),
}
CAPABILITIES = {**CORE_CAPABILITIES, **NEW_CAPABILITIES}
