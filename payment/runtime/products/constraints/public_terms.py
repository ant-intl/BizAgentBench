"""Public English rendering of structured payment release conditions."""


NOUNS = {
    "desk_portfolios": "receivables portfolio",
    "onboarding_applications": "company application",
    "rail_sites": "collection site",
}
PAYMENTS = {
    "TREASURY": "treasury",
    "PURPOSE": "purpose-designated",
    "CURRENCY": "currency delivery",
}


def clauses(conditions):
    out = []
    for c in conditions:
        if c["kind"] == "field":
            assert c["field"] == "status" and c["table"] in NOUNS, ("Unsupported contract field", c)
            out.append(f"The {NOUNS[c['table']]} {c['id']} must have status {c['value']}.")
        else:
            assert (
                c["kind"] == "sum"
                and c["table"] == "desk_payments"
                and c["field"] == "amount_minor"
                and not c.get("dedup_by")
            ), ("Unsupported contract sum", c)
            f = c["filters"]
            key = "obligation_id" if "obligation_id" in f else "instruction_id"
            assert set(f) == {"kind", "status", key} and f["status"] == "SETTLED"
            ref = "obligation" if key == "obligation_id" else "instruction"
            out.append(
                f"Settled {PAYMENTS[f['kind']]} payments for {ref} {f[key]} must total at least USD {c['minimum']/100:.2f}."
            )
    return out or ["No prior business completion is required for this drawing."]
