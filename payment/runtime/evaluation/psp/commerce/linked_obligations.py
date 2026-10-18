PRODUCTS = {
    43: ("subscription_consents", "manage_subscription_consent", "network_cards"),
    44: ("carrier_fee_claims", "manage_carrier_fee_claim", "cod_batches"),
    45: ("tax_registrations", "manage_tax_registration", "tax_batches"),
    46: ("interaccount_matches", "manage_interaccount_match", "statements"),
    47: ("recovery_period_bindings", "manage_recovery_period_binding", "executions"),
    48: ("receivable_disputes", "manage_receivable_dispute", "negotiations"),
    49: ("beneficiary_delivery_terms", "manage_beneficiary_delivery_terms", "route_offers"),
    50: ("wallet_purchase_orders", "manage_wallet_purchase_order", "controllers"),
}


def wrap(previous):
    return lambda w, c: verify(w, c, previous)


def verify(world, c, base_verify):
    n = c["number"]
    table, _, _ = PRODUCTS[n]
    ident = c["acceptance"]["linked_contract_id"]
    r = world["xp_c_" + table][ident]
    src = c["initial_world"]["xp_c_" + table][ident]
    bank = src["_bank"]
    source = world[r["source_collection"]][r["source_id"]]
    result = base_verify(world, c)
    checks = result["checks"]
    journal = [
        j
        for j in world.get("xp_journal", {}).values()
        if j.get("customer_id") == r["customer_id"] and j.get("record_type") == "cash_posting"
    ]

    def money(kind):
        return sum((j["amount_minor"] for j in journal if j["kind"] == kind))

    def check(label, passed, detail, why):
        checks.append(
            dict(
                name=f"c{n}_linked_{label}",
                passed=bool(passed),
                detail=detail,
                explanation={"check_zh": why, "rule_documents": [f"PX-C-{n}"]},
            )
        )

    if n == 43:
        check(
            "rights",
            not bank["pending_capture_minor"] or r.get("capture_voided"),
            r.get("capture_voided"),
            "Old pre-authorization is independently revoked, Cannot merely cancel future renewals",
        )
        check(
            "match",
            all((r.get("consents", {}).get(m) for m in src["essential_merchants"]))
            and r.get("consent_generation") == source["generation"]
            and (r.get("consent_limits") == src["service_limits"]),
            r.get("consents"),
            "Valid authorization for necessary services is bound to the actual current card generation",
        )
        check(
            "accounting",
            money("old_subscription_capture") == 0,
            money("old_subscription_capture"),
            "Unneeded old merchant capture has no debit.",
        )
    elif n == 44:
        orders = list(c["initial_world"]["xp_c_cod_orders"].values())
        net = sum((o["collected_from_buyer_minor"] - o["commission_minor"] for o in orders))
        check(
            "rights",
            r.get("accepted_extra_fee_minor", 0) == 0 and (not r.get("carrier_claim_waived")),
            r.get("accepted_extra_fee_minor"),
            "Unpaid returned items have no new fees, Carrier shortage claim is not waived",
        )
        check(
            "accounting",
            r.get("commission_offset_minor") == sum((o["commission_minor"] for o in orders))
            and r.get("allocated_minor") == net
            and (money("carrier_extra_fee") == 0),
            r.get("allocated_minor"),
            "Contract commission and actual net recovery are calculated item by item from the original package",
        )
        check(
            "match",
            r.get("allocation_kind") == "buyer_invoices",
            r.get("allocation_kind"),
            "Receipt offsets actual buyer invoice rather than serving as new business prepayment",
        )
    elif n == 45:
        check(
            "rights",
            r.get("registration") == src["destination"]
            and r.get("credit_assigned_jurisdiction") == bank["credit_jurisdiction"],
            r.get("registration"),
            "Post-address-change tax payment eligibility matches the tax credit jurisdiction",
        )
        check(
            "accounting",
            r.get("credit_used") == bank["credit_minor"]
            and sum((x.get("credit_minor", 0) for x in world["xp_c_tax_remittances"].values()))
            == bank["credit_minor"],
            r.get("credit_used"),
            "Prepaid credits are actually applied to current tax., Actually occupies less same-amount cash",
        )
        check(
            "match",
            r.get("credit_schedule_published")
            and r.get("credit_preserved_minor") == bank["credit_minor"],
            r.get("credit_preserved_minor"),
            "Tax settlement report lists complete credit entitlements and usage.",
        )
    elif n == 46:
        check(
            "rights",
            r.get("advice_retained") and r.get("consolidated_published"),
            r.get("advice_retained"),
            "Retain transfer source and issue combined closing of the two accounts",
        )
        check(
            "match",
            r.get("matched_reference") == bank["bank_transfer_reference"]
            and r.get("pair_direction") == "opposite",
            r.get("matched_reference"),
            "Match the opposite direction of the same transfer across accounts, Rather than unrelated same-amount receipts and payments",
        )
        check(
            "accounting",
            r.get("eliminated_minor") == bank["debit_minor"] == bank["credit_minor"]
            and r.get("consolidated_category") == "internal_transfer"
            and (r.get("external_income_minor") == 2500),
            r.get("eliminated_minor"),
            "Consolidated revenue excludes internal transfers, Do not modify the independent bank amount",
        )
    elif n == 47:
        returned = world["xp_c_executions"][src["return_execution"]]
        check(
            "rights",
            r.get("historical_preserved") and r.get("posting_period") == bank["open_period"],
            r.get("posting_period"),
            "New repairs are recorded in the current open period, Existing locked history is not reopened",
        )
        check(
            "accounting",
            r.get("restored_payable_minor") == returned["amount_minor"]
            and sum(
                (
                    x["open_minor"]
                    for x in world.get("xp_c_restored_supplier_payables", {}).values()
                    if x["execution_id"] == returned["id"]
                )
            )
            == returned["amount_minor"]
            and (r.get("return_reference") == returned.get("return_reference")),
            r.get("restored_payable_minor"),
            "Restore supplier payables based on actual returned remittance",
        )
        check(
            "match",
            r.get("rollforward_published"),
            r.get("rollforward_published"),
            "Release two executed cash entries, Payables and returned remittance change schedule",
        )
    elif n == 48:
        check(
            "rights",
            r.get("claim_preserved")
            and (not r.get("recourse_assumed"))
            and (not r.get("claim_closed")),
            r.get("claim_preserved"),
            "Retain disputed claims and assume no financing recourse",
        )
        check(
            "accounting",
            money("committed_receivable")
            == bank["undisputed_minor"]
            == r.get("receipt_allocated_minor"),
            {"received": money("committed_receivable"), "acknowledged": bank["undisputed_minor"]},
            "Currently collectible cash is only the undisputed portion confirmed by the debtor",
        )
        check(
            "match",
            r.get("allocated_reference") == bank["debt_reference"],
            r.get("allocated_reference"),
            "Actual credited amount attributed to its original receivable segment",
        )
    elif n == 49:
        total = (
            -money("correspondent_delivery_fee")
            + sum((x["fee_minor"] for x in world["xp_c_route_inquiries"].values()))
            + source.get("route_fee_minor", 0)
        )
        check(
            "rights",
            source.get("beneficiary_account") == bank["validated_account"]
            and source.get("beneficiary_received_minor") == source["principal_minor"],
            {
                "account": source.get("beneficiary_account"),
                "received": source.get("beneficiary_received_minor"),
            },
            "The actual legitimate beneficiary account receives the full original principal",
        )
        check(
            "accounting",
            total <= source["fee_cap_minor"],
            total,
            "Routing, Paid verification and collection agency fees combined are within the same budget.",
        )
        check(
            "match",
            r.get("delivery_confirmed")
            and r.get("received_principal_minor") == source.get("beneficiary_received_minor")
            and (source.get("paid_minute", 10**9) <= r.get("quote_expires_minute", -1)),
            r.get("received_principal_minor"),
            "Actual delivery receipt is consistent with the quote validity period and the fact of payment receipt",
        )
    else:
        check(
            "rights",
            r.get("cancelled_reference") == bank["cancelled_order_reference"]
            and r.get("merchant_reserved_minor") == source["purchase_minor"],
            r.get("cancelled_reference"),
            "Cancel only genuinely duplicate orders, And retain sufficient resources for still-effective procurement",
        )
        check(
            "accounting",
            r.get("captured_minor") == source["purchase_minor"]
            and r.get("capture_reference") == bank["required_order_reference"],
            r.get("captured_minor"),
            "Automatic wallet actual deduction offsets correct merchant order",
        )
        check(
            "match",
            r.get("settlement_priority") == "MERCHANT",
            r.get("settlement_priority"),
            "Merchant obligations take precedence over freely collectible funds",
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
