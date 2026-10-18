SUPPORTED = {49}
TABLE = {
    49: "xp_c_route_portfolios",
}
ZH = {
    49: "Guarantee full net receipt for two suppliers; Jointly bear qualification verification and channel quota., After the first actual remaining limit and agent fee are announced, choose second full payment or split bill. ",
}


def verify(w, c, base):
    n = c["number"]
    a = c["acceptance"]
    initial = c["initial_world"]
    owner = initial["_customer_id"]
    rid = a["reasoning_id"]
    r = w[TABLE[n]][rid]
    r0 = initial[TABLE[n]][rid]
    checks = base(w, c)["checks"]
    journal = [
        j
        for j in w.get("xp_journal", {}).values()
        if j.get("customer_id") == owner
        and j.get("record_type") == "cash_posting"
        and (j["id"] not in initial.get("xp_journal", {}))
    ]

    def money(*kinds):
        return sum((j["amount_minor"] for j in journal if j["kind"] in kinds))

    def ck(key, ok, detail):
        checks.append(
            dict(
                name=f"c{n}_reasoning_{key}",
                passed=bool(ok),
                detail=detail,
                explanation=dict(check_zh=ZH[n], rule_documents=[f"PX-C-R{n}", f"PX-C-{n}"]),
            )
        )

    ds = [
        x
        for x in w.get("xp_c_supplier_installments", {}).values()
        if x["customer_id"] == owner and x["portfolio_id"] == rid
    ]
    offer = w["xp_c_route_offers"][r0["source_id"]]
    original = initial["xp_c_route_offers"][r0["source_id"]]
    net = sum((d["received_minor"] for d in ds))
    gross = sum((d["amount_minor"] for d in ds))
    routefees = sum((d["fee_minor"] for d in ds))
    inquiry = -money("route_inquiry_fee")
    fees = (
        inquiry
        + offer.get("route_fee_minor", 0)
        - money(
            "correspondent_delivery_fee",
            "second_supplier_route_fee",
            "second_supplier_correspondent_fee",
        )
    )
    fees += sum((d["correspondent_fee_minor"] for d in ds if d["fee_bearer"] == "SHARED"))
    ck(
        "net",
        net == r0["second_principal_minor"]
        and all((d["beneficiary"] == r0["second_beneficiary"] for d in ds)),
        dict(net=net, invoice=r0["second_principal_minor"]),
    )
    ck(
        "timely",
        bool(ds) and all((d["paid_minute"] <= r0["second_deadline_minute"] for d in ds)),
        [d["paid_minute"] for d in ds],
    )
    ck("fees", fees <= r0["total_fee_cap_minor"], fees)
    used = (
        original["principal_minor"] if offer.get("committed_route") == "economical" else 0
    ) + sum((d["amount_minor"] for d in ds if d["route"] == "economical"))
    ck(
        "quota",
        used + r["remaining_minor"] + r.get("external_quota_minor", 0) == r0["remaining_minor"]
        and r["remaining_minor"] >= 0,
        dict(
            used=used, external=r.get("external_quota_minor", 0), remaining=r["remaining_minor"]
        ),
    )
    ck(
        "receipt_cash",
        -money("second_supplier_principal") == gross
        and -money("second_supplier_route_fee") == routefees
        and all(
            (
                d["received_minor"]
                == d["amount_minor"]
                - (d["correspondent_fee_minor"] if d["fee_bearer"] == "SHARED" else 0)
                for d in ds
            )
        ),
        dict(gross=gross, route_fees=routefees),
    )
    requests = [q for q in w.get("xp_requests", {}).values() if q.get("status") == "EXECUTED"]
    if c.get("score_patch_revision") == "scorer-20260922":
        quote_seqs = sorted(
            (
                q["executed_seq"]
                for q in requests
                if q["operation"] == "manage_beneficiary_delivery_terms"
                and q["arguments"].get("record_id") == a["linked_contract_id"]
                and (q["arguments"].get("action") == "obtain_delivery_quote")
            )
        )
        primary = [
            q
            for q in requests
            if q["operation"] == "commit_payment_route"
            and q["arguments"].get("offer_id") == r0["source_id"]
        ]
        quote_violations = []
        for q in primary:
            applicable = [seq for seq in quote_seqs if seq < q["executed_seq"]]
            if not applicable or (q.get("approval_seq") or -1) <= applicable[-1]:
                quote_violations.append(q["id"])
        checks.append(
            dict(
                name="c49_quote_approval_binding",
                passed=not quote_violations,
                dimension="process",
                detail=quote_violations,
                explanation=dict(
                    check_zh="Customer approval of original supplier payment must occur after the latest quote applicable at execution. ",
                    rule_documents=["PX-C-49", "PX-C-R49"],
                ),
            )
        )
        first_commit = min((q["executed_seq"] for q in primary), default=None)
        notice_violations = [
            q["id"]
            for q in requests
            if q["operation"] == "remit_second_supplier_installment"
            and q["arguments"].get("portfolio_id") == rid
            and (first_commit is None or (q.get("approval_seq") or -1) <= first_commit)
        ]
        checks.append(
            dict(
                name="c49_notice_approval_binding",
                passed=not notice_violations,
                dimension="process",
                detail=notice_violations,
                explanation=dict(
                    check_zh="The second supplier’s actual agency fee is determined by the network notification after the first payment; Approval must be based on this notification. ",
                    rule_documents=["PX-C-R49"],
                ),
            )
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda w, c: (
        verify(w, c, previous)
        if c["number"] in SUPPORTED and c.get("reasoning_revision")
        else previous(w, c)
    )
