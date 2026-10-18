SUPPORTED = {47}


def verify(world, c, base_verify):
    if not c.get("reasoning_revision") or c["number"] not in SUPPORTED:
        return base_verify(world, c)
    n = c["number"]
    initial = c["initial_world"]
    a = c["acceptance"]
    owner = initial["_customer_id"]
    rid = a["reasoning_id"]
    checks = base_verify(world, c)["checks"]
    if n == 47:
        checks = [x for x in checks if x["name"] != "c47_linked_accounting"]

    def rows(table):
        return [r for r in world.get(table, {}).values() if r.get("customer_id") == owner]

    j = [
        x
        for x in rows("xp_journal")
        if x.get("record_type") == "cash_posting" and x["id"] not in initial.get("xp_journal", {})
    ]

    def amount(kind):
        return sum((x["amount_minor"] for x in j if x["kind"] == kind))

    def ck(name, ok, detail, why):
        checks.append(
            dict(
                name=f"c{n}_reasoning_{name}",
                passed=bool(ok),
                detail=detail,
                explanation={"check_zh": why, "rule_documents": [f"PX-C-R{n}", f"PX-C-{n}"]},
            )
        )

    r = world["xp_c_supplier_advances"][rid]
    r0 = initial["xp_c_supplier_advances"][rid]
    applications = [
        x
        for x in rows("xp_c_advance_applications")
        if x["advance_id"] == rid and x["status"] == "APPLIED"
    ]
    deliveries = [x for x in rows("xp_c_calloff_deliveries") if x["advance_id"] == rid]
    applied = sum((x["amount_minor"] for x in applications))
    used = sum((x["advance_used_minor"] for x in deliveries))
    units = sum((x["units"] for x in deliveries))
    p = next(
        (
            x
            for x in rows("xp_c_restored_supplier_payables")
            if x["execution_id"] == r0["return_execution"]
        ),
        None,
    )
    original = initial["xp_c_executions"][r0["return_execution"]]["amount_minor"]
    needed = r0["_required_units"] * r0["unit_price_minor"]
    ck(
        "delivery",
        units == r0["_required_units"] and used == needed,
        dict(units=units, advance=used),
        "The complete next batch delivery actually consumes the same prepayment, Cannot retain only one plan flag",
    )
    ck(
        "allocation",
        applied == min(original, r0["original_minor"] - needed)
        and p is not None
        and (p["open_minor"] + applied == original),
        dict(applied=applied, payable=None if p is None else p["open_minor"]),
        "Restored debt minus actual offset equals remaining payable, Release all available prepayments other than delivery before applying them to debt offset",
    )
    ck(
        "conservation",
        r["available_minor"] + applied + used == r0["original_minor"]
        and r["available_minor"] >= 0,
        dict(remaining=r["available_minor"], applied=applied, delivery=used),
        "Original supplier assets in remainder, Complete conservation at three points for debt offset and goods delivered",
    )
    binding = world["xp_c_recovery_period_bindings"][r0["binding_id"]]
    ck(
        "recognition",
        binding.get("restored_payable_minor") == original
        and p is not None
        and (
            p["return_reference"]
            == world["xp_c_executions"][r0["return_execution"]]["return_reference"]
        ),
        binding.get("restored_payable_minor"),
        "Gross debt restored by the original bank return and source retention, Offset only changes actual open balance",
    )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda w, c: (
        verify(w, c, previous)
        if c["number"] in SUPPORTED and c.get("reasoning_revision")
        else previous(w, c)
    )
