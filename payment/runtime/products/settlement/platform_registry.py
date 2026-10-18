from payment.runtime.products.settlement.models import Mechanism

REGISTRY = [
    Mechanism(
        "platform_campaign_cofunding",
        "propose_platform_campaign",
        "Prepare a campaign routing election, creative payment, sponsor award, actual agency cash adjustment, sponsor remittance or merchant owner contribution.",
        "platform_campaign",
        "PlatformOperations",
        "platform_instruction",
        "platform_campaign_cofunding_event",
        "platform_event",
    ),
    Mechanism(
        "platform_seller_transfer",
        "propose_platform_transfer",
        "Prepare a legacy fulfillment source, legal stock transfer, obligation-specific customer credit novation, replacement delivery or actual seller/consignor commission settlement.",
        "platform_transfer",
        "PlatformOperations",
        "platform_instruction",
        "platform_seller_transfer_event",
        "platform_event",
    ),
    Mechanism(
        "platform_tradein_replacement",
        "propose_platform_tradein",
        "Prepare intact collection, serial retirement or return, certificate-backed stock release, model-specific order fulfillment or household trade-in benefit payment.",
        "platform_tradein",
        "PlatformOperations",
        "platform_instruction",
        "platform_tradein_replacement_event",
        "platform_event",
    ),
]
