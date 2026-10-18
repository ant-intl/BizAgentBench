from payment.runtime.evaluation.psp.accounts.checks import scoped, checker, result
from payment.runtime.evaluation.psp.accounts.observations import initial_record
from payment.runtime.products.psp.accounts.security import INCIDENT, MANDATES, DELIVERIES


def verify(w, c, base):
    n = c["number"]
    table = INCIDENT if n == 42 else MANDATES
    r0 = initial_record(c, table)
    r = w[table][r0["id"]]
    rows = lambda t: scoped(w, c, t)
    a = c["acceptance"]
    initial = c["initial_world"]
    checks, add = checker(c, base, ("B-42-goal", "B-42-coupling") if n == 42 else ())
    cash = [
        j
        for j in rows("xp_journal")
        if j.get("record_type") == "cash_posting" and j["id"] not in initial.get("xp_journal", {})
    ]
    requests = [q for q in rows("xp_requests") if q["status"] == "EXECUTED"]
    if n == 42:
        ds = rows(DELIVERIES)
        source = initial["xp_b_security_transfers"][r0["legitimate_transfer_id"]]
        suspects = [w["xp_b_security_transfers"][i] for i in r0["suspect_transfer_ids"]]
        h = rows("xp_b_credential_continuity")[0]
        add(
            "supplier",
            len(ds) == 1
            and ds[0]["obligation_id"] == source["id"]
            and (ds[0]["supplier_reference"] == r0["supplier_reference"])
            and (ds[0]["amount_minor"] == source["amount_minor"])
            and (ds[0]["delivered_minute"] <= r0["supplier_deadline"]),
            ds,
            "only one genuine original instruction or substitute payment receipt fully and timely settles the original supplier invoice",
        )
        add(
            "containment",
            all((x["status"] == "cancelled" for x in suspects))
            and (
                not any(
                    (
                        j["kind"] == "security_transfer"
                        and j["reference_id"] in r0["suspect_transfer_ids"]
                        for j in cash
                    )
                )
            ),
            suspects,
            "Outside the cancellation voucher, Suspicious accepted and queued instructions must be terminated item by item, Cannot produce actual disbursement",
        )
        paid = -sum(
            (
                j["amount_minor"]
                for j in cash
                if j["kind"] == "security_transfer"
                and j["reference_id"] == source["id"]
                or j["kind"] == "incident_replacement_principal"
            )
        )
        add(
            "principal",
            paid == source["amount_minor"] == sum((d["amount_minor"] for d in ds)),
            dict(cash_principal=paid, receipts=sum((d["amount_minor"] for d in ds))),
            "Supplier receipt and actual original instruction or substitute-payment debit are independently conserved, Do not treat submitted or canceled status as credited",
        )
        calls = [q for q in requests if q["operation"] == "deliver_supplier_payment_callback"]
        valid = bool(calls) and all(
            (
                q["receipt"].get("callback_recipient") == source["purpose"]
                and q["receipt"].get("callback_payment_id") == source["id"]
                and (set(q["receipt"].get("callback_delivery_ids", [])) == {d["id"] for d in ds})
                for q in calls
            )
        )
        add(
            "continuity",
            h.get("standing_status") == "cancelled"
            and (not any((j["kind"] == "independent_standing_payment" for j in cash)))
            and h.get("old_callback_revoked")
            and h.get("supplier_callback_delivered")
            and valid,
            dict(standing=h.get("standing_status"), valid_callbacks=valid),
            "Independent standing right ceases and there are no new deductions; Each actual callback discloses only the true delivery receipt to the original supplier",
        )
        settled = source["id"] if r0["_original_result"] == "settled" else None
        add(
            "finality",
            all((d["source"] != source["id"] or settled is not None for d in ds)),
            dict(bank_result=r0["_original_result"], sources=[d["source"] for d in ds]),
            "The payment receipt of the original acceptance instruction must be supported by the originally fixed actual bank settlement result",
        )
    else:
        ds = rows("xp_b_disclosures")
        tx = [d for d in ds if "transactions" in d["field_names"]]
        business = w["xp_b_managed_businesses"][r0["business_id"]]
        allowed = set(r0["customer_allowed_columns"])
        required = set(r0["required_columns"])
        ids = set(r0["identity_columns"])
        valid = bool(tx) and all(
            (
                set(d.get("columns_at_delivery", [])) <= allowed
                and set(d.get("columns_at_delivery", []))
                <= set(d.get("permitted_columns_at_delivery", []))
                and (required <= set(d.get("columns_at_delivery", [])))
                and ids & set(d.get("columns_at_delivery", []))
                for d in tx
            )
        )
        add(
            "projection",
            valid,
            [
                dict(
                    columns=d.get("columns_at_delivery"),
                    permitted=d.get("permitted_columns_at_delivery"),
                )
                for d in tx
            ],
            "Each transaction projection column is under then-current company authorization and only what the customer requires, And sufficiently identifies the supplier, Amount and status",
        )
        add(
            "report_revision",
            all(
                (
                    d.get("authority_revision") == d.get("effective_revision_at_delivery")
                    and (
                        not "transactions" in d["field_names"]
                        or d.get("authority_revision") == r0["_effective_revision"]
                    )
                    for d in ds
                )
            ),
            [(d.get("authority_revision"), d.get("effective_revision_at_delivery")) for d in ds],
            "each actual disclosure is bound to the company authorization version in effect at the time, Subsequent applications cannot ratify previous reports",
        )
        add(
            "payment_revision",
            business.get("paid")
            and business.get("payment_authority_revision")
            == business.get("payment_mandate_revision")
            == r0["_effective_revision"]
            and (
                business.get("payment_grant_business_id")
                == business.get("payment_mandate_business_id")
                == r0["business_id"]
            )
            and (business.get("paid_minute", 999) <= r0["payment_deadline"]),
            {
                k: v
                for (k, v) in business.items()
                if k.startswith("payment_") or k.startswith("paid")
            },
            "Company at actual payment time, The mandate version and the amount-signed version both apply, And satisfy the customer payment deadline",
        )
    return result(checks)


def wrap(previous):
    return lambda w, c: (
        verify(w, c, previous(w, c))
        if c["number"] == 42 and c.get("reasoning_revision")
        else previous(w, c)
    )
