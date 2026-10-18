from payment.runtime.evaluation.psp.accounts.observations import initial_record
from payment.runtime.products.psp.accounts.dispatch_and_returns import (
    TABLE,
    STOCK,
    LINES,
    TRANSFERS,
)

ORIGINAL = {
    34: "xp_b_order_fulfillments",
    35: "xp_b_platform_reconciliations",
}


def verify(w, c, base):
    n = c["number"]
    p = initial_record(c, TABLE)
    r = w[TABLE][p["id"]]
    owner = p["customer_id"]
    checks = list(base["checks"])
    old = initial_record(c, ORIGINAL[n])
    rows = lambda t: [x for x in w.get(t, {}).values() if x["customer_id"] == owner]

    def add(key, ok, detail, zh):
        checks.append(
            dict(
                name=f"B-{n}-reasoning-{key}",
                passed=bool(ok),
                detail=detail,
                explanation=dict(check_zh=zh, rule_documents=[c["required_documents"][-1]]),
            )
        )

    if n == 34:
        lines = rows(LINES)
        stocks = {x["id"]: x for x in rows(STOCK)}
        assigned = []
        valid = True
        for line in lines:
            original = c["initial_world"][LINES][line["id"]]
            ids = line.get("stock_ids", [])
            assigned += ids
            valid = (
                valid
                and line.get("delivered")
                and (line.get("delivered_minute", 10**9) <= original["deadline"])
                and (len(ids) == original["quantity"])
                and all(
                    (
                        stocks[i]["rating"] >= original["minimum_rating"]
                        and stocks[i]["socket"] == original["required_socket"]
                        for i in ids
                    )
                )
            )
        add(
            "lines",
            valid and len(assigned) == len(set(assigned)) == sum((x["quantity"] for x in lines)),
            lines,
            "Independently verify tiers against two actual delivery obligations, Quantity, No duplicate use of inventory and respective arrival times. ",
        )
        originals = sorted((x["rating"] for x in rows(STOCK) if x["origin"] == "ORIGINAL"))
        expected = sorted(p["_stock_ratings"][: old["_available_units"]])
        f = w[ORIGINAL[n]][old["id"]]
        subs = [x for x in rows(STOCK) if x["origin"] == "SUBSTITUTE"]
        offer = old["substitutes"].get(f.get("substitute"), {})
        add(
            "stock_basis",
            originals == expected
            and len(subs) == old["ordered_units"] - old["_available_units"]
            and all(
                (
                    x["rating"] == offer.get("rating") and x["socket"] == offer.get("socket")
                    for x in subs
                )
            ),
            stocks,
            "Physical inventory corresponds to original inspection results and actual purchased substitute models, Do not complete orders by inflating records. ",
        )
    else:
        transfers = rows(TRANSFERS)
        refunds = [x for x in transfers if x["kind"] == "CUSTOMER_REFUND"]
        sweeps = [x for x in transfers if x["kind"] == "TREASURY"]
        journal = [j for j in rows("xp_journal") if j.get("record_type") == "cash_posting"]
        fee_kinds = {
            "platform_hold_appeal_fee",
            "platform_refund_booking",
            "platform_refund_execution_fee",
            "platform_sweep_fee",
        }
        fees = -sum((j["amount_minor"] for j in journal if j["kind"] in fee_kinds))
        incoming = sum((j["amount_minor"] for j in journal if j["amount_minor"] > 0))
        held = -sum((j["amount_minor"] for j in journal if j["kind"] == "platform_proceeds_hold"))
        expected = incoming - held - p["_return_amount_minor"] - fees
        add(
            "return",
            sum((x["amount_minor"] for x in refunds)) == p["_return_amount_minor"]
            and all(
                (
                    x["status"] == "RECEIVED"
                    and x["received_minute"] <= p["return_due_minute"]
                    and (x["recipient_id"] == p["return_recipient_id"])
                    for x in refunds
                )
            ),
            refunds,
            "The actual refund must reach the correct customer at the platform’s final amount, projected manual collections cannot remedy overdue. ",
        )
        early = sum(
            (
                x["amount_minor"]
                for x in sweeps
                if x.get("status") == "RECEIVED"
                and x.get("received_minute", 10**9) <= p["early_due_minute"]
            )
        )
        total = sum(
            (
                x["amount_minor"]
                for x in sweeps
                if x.get("status") == "RECEIVED"
                and x.get("received_minute", 10**9) <= p["final_due_minute"]
            )
        )
        add(
            "treasury",
            early >= p["early_amount_minor"]
            and total == expected
            and (w["xp_accounts"][p["account_id"]]["balance_minor"] == 0)
            and all((x["recipient_id"] == p["treasury_recipient_id"] for x in sweeps)),
            dict(early=early, total=total, expected=expected),
            "Based on all actual receipts, Freeze and release, Net income from customer refund and fee recalculation, And separately verify actual receipts in both periods.. ",
        )
        add(
            "fees",
            fees <= p["max_fees_minor"],
            fees,
            "Recourse, Fees for scheduled or executed refunds and fund transfers share the customer cap. ",
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
