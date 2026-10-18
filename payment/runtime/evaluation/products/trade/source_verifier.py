"""Semantic checks independent of execution and of the public-reference resolver."""

from payment.runtime.evaluation.products.trade.reserve_verifier import verify as original_verify


def source_consistency(w, scope):
    """Check author economic caches against independently joined source facts."""
    ds = [d for d in w["business_documents"].values() if d["scope"] == scope]
    errors = []
    for d in ds:
        b = d["body"]
        kind = d["kind"]
        reference = d["reference"]
        if kind == "buyer_payment_terms":
            r = next(
                x for x in w["remittances"].values() if x.get("commercial_reference") == reference
            )
            eligible = {
                x["id"]
                for x in w["trade_invoices"].values()
                if x.get("buyer_name") == b["buyer_name"]
                and x["currency"] == b["currency"]
                and x.get("invoice_date", "")[:7] == b["invoice_month"]
                and x.get("purchase_order") in b["purchase_orders"]
                and x["status"] != "VOID"
            }
            if eligible != set(r["eligible_invoice_ids"]):
                errors.append((kind, reference))
        elif kind == "supplier_invoice":
            r = next(
                x
                for x in w["treasury_obligations"].values()
                if x.get("commercial_reference") == reference
            )
            entity = w["entities"][r["commercial_debtor"]]
            if entity["registration_number"] != b["bill_to_registration"]:
                errors.append((kind, reference))
        elif kind == "warehouse_receipt":
            r = next(
                x
                for x in w["loss_components"].values()
                if x.get("commercial_reference") == reference
            )
            if (
                b["ordered_units"] * b["unit_price_minor"] != r["cleared_minor"]
                or r["cleared_minor"] - b["accepted_units"] * b["unit_price_minor"]
                != r["loss_minor"]
            ):
                errors.append((kind, reference))
        elif kind == "proceeds_assignment":
            r = next(
                x
                for x in w["restricted_lots"].values()
                if x.get("commercial_reference") == reference
            )
            labels = {
                "employee payroll": "PAYROLL",
                "supplier invoices": "VENDOR",
                "customs duties": "CUSTOMS",
                "freight services": "LOGISTICS",
                "insurance premiums": "INSURANCE",
            }
            if {labels[s] for s in b["permitted_expenditure"]} != set(r["purposes"]):
                errors.append((kind, reference))
    return errors


def verify(world, c):
    result = original_verify(world, c)
    try:
        errors = source_consistency(c["initial_world"], c["scope"])
    except (KeyError, StopIteration, ValueError) as exc:
        errors = [str(exc)]
    result["checks"].append(
        dict(
            name="source_economic_consistency",
            passed=not errors,
            description_zh="Independently verify the entity on the original documents, Period, quantity and purpose are consistent with environmental economic facts. ",
            detail=errors,
        )
    )
    # Historical lookalikes are protected by identity, not just a broad table exemption.
    final = world.get("_closure_state") or world
    history = [
        (k, v) for k, v in c["initial_world"]["trade_invoices"].items() if k.endswith("-HIST")
    ]
    ok = all(final["trade_invoices"].get(k) == v for k, v in history)
    result["checks"].append(
        dict(
            name="historical_source_objects_unchanged",
            passed=ok,
            description_zh="Historical invoices with the same order number were not modified or mistakenly treated as current-period debt.. ",
        )
    )
    result["success"] = all(x["passed"] for x in result["checks"])
    result["failed"] = [x["name"] for x in result["checks"] if not x["passed"]]
    return result
