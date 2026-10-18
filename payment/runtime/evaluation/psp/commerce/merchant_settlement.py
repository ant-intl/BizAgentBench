SUPPORTED = {43}
TABLE = {
    43: "essential_captures",
}
ZH = {
    43: "Maintain two necessary services after card replacement, Terminate unneeded subscriptions; Combine old card pending deductions and new card statement for accounting, Avoid duplicate payments or unnecessary prepayments. ",
}


def verify(w, c, base):
    n = c["number"]
    a = c["acceptance"]
    initial = c["initial_world"]
    rid = a["reasoning_id"]
    owner = initial["_customer_id"]
    r = w["xp_c_" + TABLE[n]][rid]
    r0 = initial["xp_c_" + TABLE[n]][rid]
    checks = base(w, c)["checks"]

    def rows(t):
        return [x for x in w.get("xp_c_" + t, {}).values() if x.get("customer_id") == owner]

    cash = [
        x
        for x in w.get("xp_journal", {}).values()
        if x.get("customer_id") == owner
        and x.get("record_type") == "cash_posting"
        and (x["id"] not in initial.get("xp_journal", {}))
    ]

    def money(kind):
        return sum((x["amount_minor"] for x in cash if x["kind"] == kind))

    def ck(key, ok, detail):
        checks.append(
            dict(
                name=f"c{n}_reasoning_{key}",
                passed=bool(ok),
                detail=detail,
                explanation={"check_zh": ZH[n], "rule_documents": [f"PX-C-R{n}", f"PX-C-{n}"]},
            )
        )

    if n == 43:
        checks = [x for x in checks if x["name"] != "c43_amount"]
        paid = -money("merchant_recurring_charge") - money("essential_old_capture")
        faces = sum((initial["xp_c_network_tokens"][i]["amount_minor"] for i in a["essential"]))
        ck(
            "net_payment",
            paid == faces + r0.get("authorized_advance_minor", 0)
            and all((w["xp_c_network_tokens"][i]["billed"] for i in a["essential"]))
            and (
                r["status"] == "VOIDED"
                or r.get("treatment") == "invoice_offset"
                or (
                    r0.get("authorized_advance_minor", 0) == r0["capture_minor"]
                    and r.get("treatment") == "separate_advance"
                )
            ),
            dict(total=paid, face=faces, capture=r["status"], allocation=r.get("treatment")),
        )
    else:
        plan = next(
            (p for p in rows("onward_booking_plans") if p["id"] == r.get("booking_plan")), None
        )
        actual = r0["_incidentals_minor"]
        ck(
            "deposit",
            r.get("settled")
            and r.get("settled_minor") == actual
            and (r.get("returned_minor", 0) + r.get("offset_minor", 0) == r0["deposit_minor"])
            and (money("hotel_deposit_release") == r.get("returned_minor"))
            and (-money("hotel_legitimate_checkout") + r.get("offset_minor", 0) == actual),
            dict(returned=r.get("returned_minor"), offset=r.get("offset_minor")),
        )
        ck(
            "booking",
            plan is not None
            and plan["hotel"] == r0["booking_hotel"]
            and (-money("onward_hotel_booking") == plan["price_minor"]),
            None if plan is None else plan["hotel"],
        )
        ck(
            "fee",
            plan is not None
            and plan["price_minor"] - r0["booking_base_minor"] <= r0["optional_fee_cap_minor"],
            None if plan is None else plan["price_minor"],
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda w, c: (
        verify(w, c, previous)
        if c["number"] in SUPPORTED and c.get("reasoning_revision")
        else previous(w, c)
    )
