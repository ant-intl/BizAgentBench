from payment.runtime.products.settlement.models import Mechanism

REGISTRY = [
    Mechanism(
        "queued_pvp_liquidity",
        "propose_pvp_queue",
        "Prepare a queued currency settlement, a gross release, a registered funding ticket, a term placement or a supplier remittance.",
        "settlement_pvp",
        "PvPOperations",
        "pvp_instruction",
        "queued_pvp_liquidity_event",
        "pvp_event",
    ),
    Mechanism(
        "participant_default_waterfall",
        "propose_default_waterfall",
        "Prepare an actual member loss contribution, collateral-source election, emergency-liquidity draw or repayment, surviving settlement payment or estate-certificate use.",
        "settlement_default",
        "DefaultOperations",
        "default_instruction",
        "participant_default_waterfall_event",
        "default_event",
    ),
    Mechanism(
        "documentary_proceeds_priority",
        "propose_documentary_assignment",
        "Prepare a documentary proceeds assignment, performance transfer, presentation, revocation or actual debt payment.",
        "settlement_assignment",
        "AssignmentOperations",
        "assignment_instruction",
        "documentary_proceeds_priority_event",
        "assignment_event",
    ),
    Mechanism(
        "beneficial_sweep_restitution",
        "propose_beneficial_restitution",
        "Prepare an actual source-bound sweep reversal, authorized bilateral loan, owned supplier or bank-debt remittance, or a buyer-deposit return.",
        "settlement_pool",
        "PoolOperations",
        "pool_instruction",
        "beneficial_sweep_restitution_event",
        "pool_event",
    ),
]
