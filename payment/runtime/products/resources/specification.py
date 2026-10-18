from payment.runtime.products.reconciliation.specification import (
    CAPABILITIES as OLD_CAPABILITIES,
    TABLES as OLD_TABLES,
    EXTRA_TABLES as OLD_EXTRA,
    cap,
)

RESOURCE_TABLES = ("capacity_pools", "capacity_obligations", "capacity_reservations")
EXTRA_TABLES = OLD_EXTRA + RESOURCE_TABLES
TABLES = OLD_TABLES + RESOURCE_TABLES
NEW_CAPABILITIES = {
    "propose_capacity_reservations": cap(
        "shared-capacity",
        "Atomically reserve an explicit caller-supplied allocation map: obligation ID to pool ID to integer quantity. This executes the supplied allocation and does not select or optimize a plan.",
        allocations="object",
    ),
    "propose_capacity_reservation": cap(
        "shared-capacity",
        "Reserve a stated quantity of operational coverage from one pool for one obligation. Actual pool debit follows its published coverage ratio. A reservation is not completion.",
        pool_id="string",
        obligation_id="string",
        quantity="integer",
    ),
    "propose_capacity_release": cap(
        "shared-capacity",
        "Release an unused operational coverage reservation. Consumed coverage cannot be released through this tool.",
        reservation_id="string",
    ),
}
CAPABILITIES = {**OLD_CAPABILITIES, **NEW_CAPABILITIES}
