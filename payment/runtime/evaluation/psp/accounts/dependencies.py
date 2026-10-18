from payment.runtime.evaluation.psp.accounts import (
    foreign_exchange as b_reasoning_fx,
    recovery as b_reasoning_recovery,
    refund_and_disclosure as b_reasoning_refund_disclosure,
    dispatch_and_returns as b_reasoning_root,
    royalty as b_reasoning_royalty,
    security as b_reasoning_security,
    tax_handover as b_reasoning_tax_handover,
)

MODULES = {
    34: b_reasoning_root,
    35: b_reasoning_root,
    37: b_reasoning_royalty,
    41: b_reasoning_recovery,
    38: b_reasoning_tax_handover,
    36: b_reasoning_fx,
    39: b_reasoning_refund_disclosure,
    40: b_reasoning_refund_disclosure,
    42: b_reasoning_security,
}


def wrap(previous):
    return lambda w, c: (
        MODULES[c["number"]].verify(w, c, previous(w, c))
        if c.get("reasoning_revision") and c["number"] in MODULES
        else previous(w, c)
    )
