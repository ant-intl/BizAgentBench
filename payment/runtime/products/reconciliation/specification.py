from payment.runtime.products.customer_access.specification import (
    CAPABILITIES as OLD_CAPABILITIES,
    TABLES as OLD_TABLES,
    P,
    cap,
)

EXTRA_TABLES = (
    "bridge_facilities",
    "credit_redemptions",
    "legalization_slots",
    "marketplace_adjustments",
    "portal_delegates",
    "export_declarations",
    "export_allocations",
    "volume_certificates",
    "return_routes",
    "return_allocations",
    "card_limits",
    "fee_guarantees",
)
TABLES = OLD_TABLES + EXTRA_TABLES
NEW_CAPABILITIES = {
    "propose_bridge_draw": cap(
        "bridge",
        "Draw a receivable bridge under the selected facility; collateral and repayment remain live.",
        facility_id="string",
        account_id="string",
        amount_minor="integer",
    ),
    "propose_bridge_repayment": cap(
        "bridge",
        "Repay the entire outstanding bridge from its borrowing account.",
        facility_id="string",
    ),
    "propose_credit_redemption": cap(
        "redemption",
        "Redeem a supplier credit using EARLY or SETTLEMENT terms.",
        note_id="string",
        method="string",
    ),
    "propose_legalization_booking": cap(
        "legalization",
        "Reserve a CURRENT or FORWARD legalization slot before intake closes.",
        slot_id="string",
        mode="string",
    ),
    "propose_letter_legalization": cap(
        "legalization",
        "Submit an issued bank letter to a reserved legalization slot.",
        slot_id="string",
        letter_id="string",
    ),
    "propose_marketplace_adjustment": cap(
        "adjustments",
        "Book a notified marketplace debit as SALES_RETURN or SERVICE_FEE.",
        adjustment_id="string",
        classification="string",
    ),
    "propose_delegate_adoption": cap(
        "delegates",
        "Bind a delegate to the currently SMS-verified account security context.",
        delegate_id="string",
    ),
    "propose_delegate_revocation": cap(
        "delegates",
        "Revoke a Portal delegate; recovery ownership and SMS proof are affected.",
        delegate_id="string",
    ),
    "propose_export_allocation": cap(
        "declarations",
        "Reserve current declaration capacity for an account payment.",
        declaration_id="string",
        account_id="string",
        amount_minor="integer",
    ),
    "propose_export_release": cap(
        "declarations", "Release an unconsumed declaration allocation.", allocation_id="string"
    ),
    "propose_volume_certificate": cap(
        "volume",
        "Certify CURRENT_ROLLING or LOCKED_PERIOD qualifying source rows and active membership.",
        group_id="string",
        basis="string",
    ),
    "propose_return_routing": cap(
        "return-routing",
        "Set DIRECT or AGGREGATE routing for a traced return.",
        incoming_id="string",
        route_id="string",
    ),
    "propose_return_allocation": cap(
        "return-routing",
        "Credit a settled aggregate return to a dated buyer allocation instruction.",
        refund_id="string",
        instruction_id="string",
    ),
    "propose_card_limit_transfer": cap(
        "card-limits",
        "Reallocate daily purchase capacity between cards in one company pool.",
        source_id="string",
        destination_id="string",
        amount_minor="integer",
    ),
    "propose_fee_guarantee": cap(
        "fee-guarantee",
        "Deposit a refundable tariff guarantee under CASH or MARGIN coverage.",
        account_id="string",
        coverage="string",
        amount_minor="integer",
    ),
    "propose_fee_guarantee_release": cap(
        "fee-guarantee",
        "Cancel an unbound deposit or release it after its remittance settles.",
        guarantee_id="string",
    ),
}
CAPABILITIES = {**OLD_CAPABILITIES, **NEW_CAPABILITIES}
