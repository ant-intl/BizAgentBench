from payment.runtime.products.settlement.models import Mechanism

REGISTRY = [
    Mechanism(
        "custody_coupon_entitlements",
        "propose_custody_coupon",
        "Prepare a manufactured-coupon election, coupon advance, bond settlement, investor coupon transfer, authorized excess recovery, or guarantor collateral application and return.",
        "custody_coupon",
        "CustodyCouponOperations",
        "custody_coupon_instruction",
        "custody_coupon_entitlements_event",
        "custody_coupon_event",
    ),
    Mechanism(
        "custody_elective_allocation",
        "propose_custody_election",
        "Prepare an elective exchange tender, old-share delivery, invoice payment, bonus-right redemption or subscription, or a class-specific block or odd-lot market trade.",
        "custody_election",
        "CustodyElectionOperations",
        "custody_election_instruction",
        "custody_elective_allocation_event",
        "custody_election_event",
    ),
    Mechanism(
        "custody_ndf_segregation",
        "propose_custody_ndf",
        "Prepare a bond pledge, release or delivery, segregated margin movement, financing transaction or agreement-specific NDF settlement.",
        "custody_ndf",
        "CustodyNdfOperations",
        "custody_ndf_instruction",
        "custody_ndf_segregation_event",
        "custody_ndf_event",
    ),
]
