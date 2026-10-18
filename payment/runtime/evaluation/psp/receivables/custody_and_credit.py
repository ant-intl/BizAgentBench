from copy import deepcopy
from payment.runtime.evaluation.psp.receivables.checks import rr, check

DOCS = {
    31: ["PX-A-R-CUSTODY-ONBOARDING", "PX-A-R-CUSTODY-CASH"],
    33: ["PX-A-R-CREDIT-NETTING", "PX-A-R-CREDIT-CASH"],
}


def verify(world, c, previous):

    n = c["number"]
    original = c["initial_world"]
    owner = original["_customer_id"]
    p = rr(original, "reasoning_contracts")[0]
    shadow = deepcopy(c)

    def actual(t):
        return [x for x in world.get("xp_a_" + t, {}).values() if x["customer_id"] == owner]

    if n == 33:
        for note in actual("supplier_credit_notes"):
            if note.get("applied_minor", 0):
                shadow["initial_world"]["xp_a_invoices"][note["applied_invoice_id"]][
                    "amount_minor"
                ] -= note["applied_minor"]
    result = previous(world, shadow)
    checks = result["checks"]
    state = next((x for x in actual("reasoning_contracts") if x["id"] == p["id"]))

    def ck(key, ok, detail, why):
        check(checks, n, key, ok, detail, why, DOCS[n])

    if n == 31:
        ck(
            "custody_deadlines",
            state.get("partitioned_minute", 10**6) <= p["security_deadline"]
            and state.get("normal_paid_minute", 10**6) <= p["supplier_deadline"],
            state,
            "Verify according to actual supplier payment and guarantee transfer times, Neither review nor activation request can replace actual completion. ",
        )
        fees = -sum(
            (
                j["amount_minor"]
                for j in world["xp_journal"].values()
                if j.get("customer_id") == owner
                and j.get("record_type") == "cash_posting"
                and (j["kind"] in ("CUSTODY_REVIEW_FEE", "CUSTODY_ACTIVATION_FEE"))
            )
        )
        expected = sum(
            (
                original["xp_a_custody_services"][state[key]]["fee_minor"]
                for key in ("review_order_id", "activation_order_id")
                if state.get(key)
            )
        )
        normal = original["xp_a_invoices"][p["normal_invoice_id"]]["amount_minor"]
        ck(
            "custody_cash",
            fees == expected
            and original["xp_accounts"][p["account_id"]]["balance_minor"] >= normal + fees,
            dict(actual_fees=fees, normal_principal=normal),
            "Independently account from the original service price for own operating cash jointly occupied by actual total fees and normal principal. ",
        )
    else:
        notes = actual("supplier_credit_notes")
        truth = {x["id"]: x for x in rr(original, "supplier_credit_notes")}
        realized = sum(
            (
                x.get("applied_minor", 0) + x.get("refunded_minor", 0)
                for x in notes
                if x.get("disposed_minute", 10**6) <= p["realization_deadline"]
            )
        )
        wanted = sum((x["amount_minor"] for x in truth.values()))
        ck(
            "credit_value",
            realized == wanted
            and all(
                (
                    x.get("applied_minor", 0) + x.get("refunded_minor", 0)
                    <= truth[x["id"]]["amount_minor"]
                    for x in notes
                )
            ),
            dict(realized=realized, expected=wanted, notes=notes),
            "The original credit value must become actual principal reduction or credited cash before the customer date, Do not recognize face value consumed by combination rules or merely requested for refund. ",
        )
        fee = -sum(
            (
                j["amount_minor"]
                for j in world["xp_journal"].values()
                if j.get("customer_id") == owner
                and j.get("record_type") == "cash_posting"
                and (j["kind"] == "SUPPLIER_CREDIT_REFUND_FEE")
            )
        )
        ck(
            "credit_fees",
            fee <= p["max_credit_fees_minor"],
            fee,
            "All actual withdrawal fees together satisfy the customer cap, Do not ignore redemption of more expensive remaining credits because the offset face value is larger. ",
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
