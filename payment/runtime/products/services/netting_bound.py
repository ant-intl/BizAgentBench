"""Evaluator-side invoice arithmetic and participant debit lower bounds."""



def residual(rows, ref, when):
    invoice = rows[ref]
    deductions = invoice["netting_settled_minor"]
    applicable = [
        n
        for n in rows.values()
        if n["kind"] == "clearing_credit_note"
        and n["invoice_ref"] == ref
        and n["status"] in ("ISSUED", "FINAL", "CANCELLED")
        and n["effective_minute"] <= when
    ]
    for n in applicable:
        if n["status"] != "CANCELLED" and not any(
            z["series"] == n["series"] and z["version"] > n["version"] for z in applicable
        ):
            deductions += n["amount_minor"]
    deductions += sum(
        x["amount_minor"]
        for x in rows.values()
        if x["kind"] == "intercompany_cash_posting"
        and x["invoice_ref"] == ref
        and x["status"] == "SETTLED"
    )
    return invoice["gross_minor"] - deductions


def receipt_net(rows, amounts):
    return {
        p["reference"]: sum(
            n
            * (
                (1 if rows[ref]["creditor_ref"] == p["reference"] else 0)
                - (1 if rows[ref]["debtor_ref"] == p["reference"] else 0)
            )
            for ref, n in amounts.items()
        )
        for p in rows.values()
        if p["kind"] == "clearing_account"
    }
