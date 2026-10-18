from payment.runtime.evaluation.psp.receivables.collection_and_cash import wrap as _with_special
from payment.runtime.evaluation.psp.receivables.ownership_and_handover import (
    wrap as _with_ownership_and_handover,
)
from payment.runtime.evaluation.psp.receivables.dispatch import wrap as _with_dispatch
from payment.runtime.evaluation.psp.receivables.collection_calendar import (
    wrap as _with_collection_calendar,
)

META = {
    31: (
        "unknown_funds_owner",
        "Unknown incoming funds and impersonated refund requests.",
        "OWNER",
    ),
    32: ("incoming_recall_race", "Race condition between posting and remitter recall", "OWNER"),
    33: (
        "causal_bank_receipts",
        "Payment case closure under out-of-order bank receipts",
        "RECEIPTS",
    ),
}


def verify(world, contract):
    """Recompute obligations from original records and economic evidence, not a plan."""
    n = contract["number"]
    original = contract["initial_world"]
    terms = contract.get("acceptance", {})
    checks = []
    doc = "PX-A-" + META[n][2]
    customer = original["_customer_id"]

    def rows(table):
        return [
            r for r in world.get("xp_a_" + table, {}).values() if r.get("customer_id") == customer
        ]

    def orig(table):
        return [
            r
            for r in original.get("xp_a_" + table, {}).values()
            if r.get("customer_id") == customer
        ]

    def check(name, passed, detail, explanation):
        checks.append(
            dict(
                name="a." + str(n) + "." + name,
                passed=bool(passed),
                detail=detail,
                explanation=dict(check_zh=explanation, rule_documents=[doc]),
            )
        )

    journal = [
        x
        for x in world.get("xp_journal", {}).values()
        if x.get("record_type") == "cash_posting" and x.get("customer_id") == customer
    ]
    payments = rows("payments")
    events = [x for x in world.get("xp_events", {}).values() if x.get("customer_id") == customer]

    def total(kind):
        return sum((x["amount_minor"] for x in journal if x["kind"] == kind))

    def paid(iid, recipient=None):
        return sum(
            (
                p["amount_minor"]
                for p in payments
                if p["invoice_id"] == iid and (recipient is None or p["recipient_id"] == recipient)
            )
        )

    def exact_invoices(invoices=None):
        invoices = orig("invoices") if invoices is None else invoices
        return all(
            (
                paid(i["id"], i["recipient_id"]) == i["amount_minor"]
                and len([p for p in payments if p["invoice_id"] == i["id"]]) == 1
                for i in invoices
            )
        )

    def evidence(target, purpose):
        files = [
            x
            for x in original["_user_files"]
            if x["purpose"] == purpose and x["target_id"] == target
        ]
        return any(
            (
                s["target_id"] == target
                and s["purpose"] == purpose
                and any((s["content"] == f["content"] and s["file_id"] == f["id"] for f in files))
                for s in world.get("xp_submissions", {}).values()
            )
        )

    necessary_events = events
    if events:
        check(
            "bank_results_final",
            all((x["status"] == "COMPLETED" for x in necessary_events)),
            {x["id"]: x["status"] for x in necessary_events},
            "Pending events that only affect actual business results need to be completed.; Unrelated candidate account, Unused inventory and subsequent notices already finally delivered do not block case closure. ",
        )
    if n == 31:
        inc = rows("incoming")[0]
        final_allowed = next(
            (e for e in original["xp_events"].values() if e["customer_id"] == customer)
        )["_payload"]["updates"][0]["fields"]["allowed_dispositions"]
        expected = "CLAIM" if "CLAIM" in final_allowed else "RETURN_ORIGIN"
        disposal_ok = inc.get("disposition") == expected and (
            expected == "CLAIM"
            or inc.get("returned_to") == orig("incoming")[0]["origin_recipient_id"]
        )
        check(
            "authenticated_disposal",
            disposal_ok
            and evidence(inc["id"], "origin_proof")
            and inc.get("ownership_investigation"),
            inc,
            "Unidentified principal is claimed or returned via original route based on original source evidence and bank ownership conclusion. ",
        )
        normal = [i for i in orig("invoices") if i["name"] == "Normal inventory invoice"]
        check(
            "normal_supplier_paid",
            exact_invoices(normal),
            payments,
            "Normal supplier obligations are fully paid once from existing available funds. ",
        )
        email_ids = {
            i["id"] for i in orig("invoices") if i["name"] == "Unverified email repayment request"
        }
        check(
            "no_substitute_refund",
            not any((p["invoice_id"] in email_ids for p in payments)),
            payments,
            "Email contact who knows the amount cannot receive an alternative account payment. ",
        )
    elif n == 32:
        inc = rows("incoming")[0]
        final = next((e for e in original["xp_events"].values() if e["customer_id"] == customer))[
            "_payload"
        ]["updates"][0]["fields"]
        expected = terms["final_disposition"]
        check(
            "final_bank_disposition",
            inc.get("disposition") == expected
            and inc.get("applied_decision_version") == final["decision_version"],
            inc,
            "Execute only mutually exclusive principal dispositions permitted by the final bank version; Early acceptance does not cover later decisions. ",
        )
        expected_credit = orig("incoming")[0]["amount_minor"] if expected == "CLAIM" else 0
        check(
            "single_principal_owner",
            total("INCOMING_RELEASE") == expected_credit
            and (not payments)
            and (
                expected != "RETURN_ORIGIN"
                or inc.get("returned_minor") == orig("incoming")[0]["amount_minor"]
            ),
            dict(credit=total("INCOMING_RELEASE"), payments=payments),
            "The same principal can only be retained or refunded once, Do not cause a double outflow by separately paying a good-faith refund.. ",
        )
        check(
            "contract_claim_evidence",
            evidence(inc["id"], "contract_proof") and inc.get("claim_submitted"),
            inc.get("claim_position"),
            "Parallel recall claims must have original contract evidence and genuine submission records.. ",
        )
    elif n == 33:
        net = {
            i["id"]: paid(i["id"])
            - sum(
                (
                    x["amount_minor"]
                    for x in journal
                    if x["kind"] == "BANK_PAYMENT_RETURN" and x["reference_id"] == i["id"]
                )
            )
            for i in orig("invoices")
        }
        check(
            "supplier_net_exact",
            all((net[i["id"]] == i["amount_minor"] for i in orig("invoices"))),
            net,
            "After initial payment minus actual returned remittance plus replacement payment, Net receipts for each supplier obligation are exactly equal. ",
        )
        statuses = {}
        for inv in rows("invoices"):
            messages = [x for x in rows("bank_receipts") if x["invoice_id"] == inv["id"]]
            causal = max(messages, key=lambda x: x["bank_sequence"])
            statuses[inv["id"]] = (
                inv.get("projected_status") == causal["status"]
                and inv.get("projected_receipt_id") == causal["id"]
            )
        check(
            "causal_projection",
            all(statuses.values()),
            statuses,
            "Final-state projection is selected by causal sequence; Late old processing message cannot overwrite settled status. ",
        )
        check(
            "correct_replacement_recipient",
            all(
                (
                    p["recipient_id"]
                    == next(
                        (i["recipient_id"] for i in orig("invoices") if i["id"] == p["invoice_id"])
                    )
                    for p in payments
                )
            ),
            payments,
            "substitute payment for genuine returned remittance still pays the original contract supplier. ",
        )
    return dict(success=all((c["passed"] for c in checks)), checks=checks)


from payment.runtime.evaluation.pipeline import VerificationPipeline

verify = VerificationPipeline(
    verify,
    (
        _with_special,
        _with_ownership_and_handover,
        _with_dispatch,
        _with_collection_calendar,
    ),
)
