"""Independent final-evidence and cross-card credit calculation."""


def assessment(rows):
    finals = {}
    for row in rows.values():
        if (
            row["kind"] == "merchant_purchase_evidence"
            and row["verified_genuine"]
            and row["status"] == "FINAL"
        ):
            if row["series"] not in finals or finals[row["series"]]["version"] < row["version"]:
                finals[row["series"]] = row
    supported = set()
    presentments = [r for r in rows.values() if r["kind"] == "card_presentment"]
    for evidence in finals.values():
        matches = [
            p
            for p in presentments
            if p["entity"] == evidence["entity"]
            and p["cardholder"] == evidence["cardholder"]
            and p["merchant_case_ref"] == evidence["merchant_case_ref"]
            and p["merchant"] == evidence["merchant"]
            and evidence["valid_from_day"] <= p["service_day"] <= evidence["valid_through_day"]
            and p["settlement_trace"] in evidence["settlement_trace_refs"]
        ]
        if sum(p["amount_minor"] for p in matches) <= evidence["consent_limit_minor"]:
            supported.update(p["reference"] for p in matches)
    posted = {}
    for row in rows.values():
        if row["kind"] == "merchant_card_credit" and row["status"] == "POSTED":
            posted[row["merchant_case_ref"]] = (
                posted.get(row["merchant_case_ref"], 0) + row["amount_minor"]
            )
    claims = {}
    cases = {p["merchant_case_ref"] for p in presentments}
    for case in cases:
        unsupported = [
            p
            for p in presentments
            if p["merchant_case_ref"] == case and p["reference"] not in supported
        ]
        loss = max(0, sum(p["amount_minor"] for p in unsupported) - posted.get(case, 0))
        if loss:
            assert len(unsupported) == 1, ("Ambiguous claim allocation", case)
            claims[unsupported[0]["reference"]] = loss
    return dict(
        supported=sorted(supported), posted_credit_by_case_minor=posted, claims_minor=claims
    )
