"""Opt-in reasoning revisions; legacy business functions stay callable."""

from payment.runtime.products.psp.accounts import (
    tax_cycle as b_reasoning_tax_cycle,
    currency_settlement as b_reasoning_pvp,
    billing as b_reasoning_billing,
    campaign as b_reasoning_campaign,
    export as b_reasoning_export,
    foreign_exchange as b_reasoning_fx,
    invoice as b_reasoning_invoice,
    recovery as b_reasoning_recovery,
    refund_and_disclosure as b_reasoning_refund_disclosure,
    dispatch_and_returns as b_reasoning_root,
    royalty as b_reasoning_royalty,
    security as b_reasoning_security,
    tax_handover as b_reasoning_tax_handover,
)


def register(operations, old_event):
    event = b_reasoning_royalty.register(operations, old_event)
    event = b_reasoning_recovery.register(operations, event)
    event = b_reasoning_tax_handover.register(operations, event)
    event = b_reasoning_campaign.register(operations, event)
    event = b_reasoning_fx.register(operations, event)
    event = b_reasoning_export.register(operations, event)
    event = b_reasoning_invoice.register(operations, event)
    event = b_reasoning_pvp.register(operations, event)
    event = b_reasoning_billing.register(operations, event)
    event = b_reasoning_tax_cycle.register(operations, event)
    event = b_reasoning_security.register(operations, event)
    event = b_reasoning_refund_disclosure.register(operations, event)
    return b_reasoning_root.register(operations, event)
