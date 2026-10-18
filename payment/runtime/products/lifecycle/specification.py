from copy import deepcopy
from payment.runtime.products.settlement.specification import (
    SPECS as OLD_SPECS,
    CAPABILITIES as OLD_CAPABILITIES,
)

KINDS = [
    "split_capture",
    "authentication_binding",
    "marketplace_split_refund",
    "stored_value_restitution",
    "acquirer_batch_close",
    "subscription_proration",
    "cross_calendar_delivery",
    "installment_return",
    "group_purchase_commitment",
    "cheque_stop_and_replacement",
]
NEW = {
    "propose_payment_"
    + k: dict(
        name="propose_payment_" + k,
        description="Prepare an owner-approved "
        + k.replace("_", " ")
        + " instruction. Applicable contract documents define individual actions and their local effects.",
        parameters={"case_id": "string", "action": "string", "data": "object"},
    )
    for k in KINDS
}
CAPABILITIES = {**deepcopy(OLD_CAPABILITIES), **NEW}
SPECS = deepcopy(OLD_SPECS)
SPECS["authenticate_payment"] = dict(
    actor="user",
    read=False,
    description="Complete the named bank challenge in your private issuer portal. The bank checks your availability and records the exact merchant, order and maximum amount. This is not support approval and does not execute a payment. Use only when support asks you to verify a specific explained purchase.",
    params=[("authentication_id", str)],
)
