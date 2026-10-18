TABLES = {
    34: "xp_b_order_fulfillments",
    35: "xp_b_platform_reconciliations",
    37: "xp_b_royalty_tax",
    39: "xp_b_customs_liens",
    40: "xp_b_proof_access",
    42: "xp_b_credential_continuity",
}


def verify_extra(world, contract, add):
    n = contract["number"]
    if n not in TABLES:
        return
    initial = contract["initial_world"]
    rid = contract["acceptance"]["enhancement_record_id"]
    original = initial[TABLES[n]][rid]
    r = world.get(TABLES[n], {}).get(rid, {})
    owner = original["customer_id"]

    def rec(table, key, old=False):
        return (initial if old else world).get(table, {}).get(key, {})

    def rows(table, old=False):
        return [
            x
            for x in (initial if old else world).get(table, {}).values()
            if x.get("customer_id") == owner
        ]

    def requests(operation):
        return sorted(
            [
                x
                for x in rows("xp_requests")
                if x.get("operation") == operation and x.get("status") == "EXECUTED"
            ],
            key=lambda x: x.get("executed_seq", -1),
        )

    def events(kind):
        return [
            x
            for x in rows("xp_events")
            if x.get("kind") == kind
            and x.get("subject_id") == rid
            and (x.get("status") == "COMPLETED")
        ]

    def cash(kind):
        return sum(
            (
                x["amount_minor"]
                for x in rows("xp_journal")
                if x.get("record_type") == "cash_posting" and x.get("kind") == kind
            )
        )

    def add_result(ok, detail, reason):
        add("coupling", bool(ok), detail, reason)

    if n == 34:
        order = rec("xp_b_store_orders", original["store_order_id"])
        old_order = rec("xp_b_store_orders", original["store_order_id"], True)
        source = [
            p
            for p in rows("xp_b_gateway_payments", True)
            if p.get("merchant_account") == old_order.get("merchant_account")
            and p.get("external_reference") == old_order.get("external_reference")
        ]
        expected_net = sum(
            (p["amount_minor"] * (-1 if p.get("receipt_kind") == "refund" else 1) for p in source)
        )
        available = original["_available_units"]
        missing = original["ordered_units"] - available
        substitute = original["substitutes"].get(r.get("substitute"), {})
        compatible = not missing or (
            substitute.get("socket") == original["required_socket"]
            and substitute.get("rating", -1) >= original["required_rating"]
        )
        valid = (
            bool(events("b_enhance_inventory"))
            and r.get("available_units") == available
            and (r.get("substitute_units", 0) == missing)
            and compatible
            and bool(events("b_enhance_delivery"))
            and (r.get("delivered_units") == original["ordered_units"])
            and r.get("closed")
            and (r.get("merchant_account") == old_order.get("merchant_account"))
            and (set(r.get("paid_receipt_ids", [])) == {p["id"] for p in source})
            and (r.get("receipt_net_minor") == expected_net == old_order.get("amount_minor"))
            and (order.get("net_received_minor") == expected_net)
            and (cash("order_substitute_fee") == -substitute.get("fee_minor", 0))
        )
        add_result(
            valid,
            {
                "capture_refund_net_minor": expected_net,
                "receipt_ids": r.get("paid_receipt_ids"),
                "available_units": available,
                "substitute": substitute,
                "delivered_units": r.get("delivered_units"),
            },
            "Original capture and refund components calculated net by true merchant and order references; Out-of-stock quantity of the same paid order, Replacement socket, rated value, and carrier receipt must all be satisfied.. ",
        )
    elif n == 35:
        held = original["hold_minor"]
        receiving = rec("xp_b_receiving_accounts", original["receiving_id"])
        hold_payout = rec("xp_b_platform_payouts", r.get("hold_payout_id"))
        expected_release = held
        valid = (
            r.get("registered")
            and r.get("hold_posted")
            and (hold_payout.get("status") == "settled")
            and (hold_payout.get("receiving_id") == original["receiving_id"])
            and (cash("platform_proceeds_hold") == -held)
            and (not held or bool(events("b_enhance_reserve_review")))
            and (r.get("reserve_released_minor", 0) == expected_release)
            and (cash("platform_reserve_release") == expected_release)
            and r.get("manual_received")
            and (r.get("manual_reference") == original["manual_reference"])
            and (cash("manual_platform_receipt") == original["manual_receipt_minor"])
            and (receiving.get("status") == "active")
            and receiving.get("manual_supported")
        )
        add_result(
            valid,
            {
                "held_minor": held,
                "released_minor": cash("platform_reserve_release"),
                "manual_received_minor": cash("manual_platform_receipt"),
                "hold_payout": hold_payout.get("id"),
            },
            "Recalculate original revenue from cash withholding and release of truly accepted payments, then verify that the independent manual incoming payment has indeed been credited; Stopping automatic rules cannot be done by closing the collection account or abandoning part of original income. ",
        )
    elif n == 37:
        pool = rec("xp_b_pools", original["pool_id"])
        old_pool = rec("xp_b_pools", original["pool_id"], True)
        allocations = [
            a for a in rows("xp_b_allocations", True) if a["pool_id"] == original["pool_id"]
        ]
        total = old_pool["source_minor"] * old_pool["rate_num"] // old_pool["rate_den"]
        denominator = sum((a["weight"] for a in allocations))
        gross = {a["id"]: total * a["weight"] // denominator for a in allocations}
        ranking = sorted(
            allocations, key=lambda a: (-(total * a["weight"] % denominator), a["residual_rank"])
        )
        for a in ranking[: total - sum(gross.values())]:
            gross[a["id"]] += 1
        liable = original["beneficiary_id"]
        tax = gross.get(liable, 0) * original["_withholding_bps"] // 10000
        actual = {
            a["id"]: rec("xp_b_allocations", a["id"]).get("paid_minor", 0) for a in allocations
        }
        correct_net = all(
            (actual[k] == v - (tax if k == liable else 0) for (k, v) in gross.items())
        )
        attribution = (
            r.get("withheld_beneficiary_id") == liable
            and r.get("certificate_beneficiary_id") == liable
            if tax
            else r.get("withheld_minor", 0) == 0
        )
        valid = (
            bool(events("b_enhance_royalty_residency"))
            and r.get("withholding_bps") == original["_withholding_bps"]
            and correct_net
            and (r.get("withheld_minor", 0) == tax)
            and attribution
            and r.get("certificate_delivered")
            and (r.get("certificate_tax_minor") == tax)
            and (sum(actual.values()) + tax == total)
            and (pool.get("remaining_minor") == 0)
        )
        add_result(
            valid,
            {
                "gross": gross,
                "cash": actual,
                "liable_beneficiary": liable,
                "expected_tax_minor": tax,
                "certificate_beneficiary": r.get("certificate_beneficiary_id"),
            },
            "Recalculate each person’s pre-tax share from the original integer allocation contract, Then only deduct rounded-down tax amount of actual taxpayer; Net amount per person and its tax attribution are both correct simultaneously, Cannot only verify aggregate conservation of the shared pool. ",
        )
    elif n == 39:
        importer = rec("xp_b_importers", original["importer_id"])
        dest = rec("xp_b_destinations", importer.get("destination_id"))
        lien = original["_lien_minor"]
        cert = original["release_certificates"].get(r.get("certificate_id"), {})
        release = (
            min(lien, cert.get("amount_minor", 0))
            if cert.get("entry_id") == original["_lien_entry_id"] and r.get("lien_released")
            else 0
        )
        gross = sum(
            (
                e["duty_minor"]
                for e in rows("xp_b_customs_entries", True)
                if e.get("importer_id") == original["importer_id"] and e.get("_eligible")
            )
        )
        valid = (
            bool(events("b_enhance_customs_lien"))
            and r.get("lien_minor") == lien
            and (not lien or (bool(events("b_enhance_lien_discharge")) and release == lien))
            and (r.get("release_minor", 0) == release)
            and (importer.get("award_status") == "received")
            and (dest.get("owner") == importer.get("legal_name"))
            and dest.get("ach")
            and (dest.get("received_minor") == gross)
            and (dest.get("available_minor") == gross)
            and r.get("statement_delivered")
            and (r.get("award_minor") == gross)
            and (r.get("encumbered_minor") == 0)
            and (r.get("account_id_at_delivery") == dest.get("id"))
            and (cash("customs_lien_release_fee") == -cert.get("fee_minor", 0))
        )
        add_result(
            valid,
            {
                "gross_award": gross,
                "original_lien": lien,
                "certificate": cert,
                "independently_released": release,
                "available_minor": dest.get("available_minor"),
            },
            "Eligible duties form the total award payment, The original registered lien must be removed by the same import record and sufficient authentic certificate; Same name ACH receipt and final unencumbered available amount hold simultaneously, A lawful release on another record shall not substitute.. ",
        )
    elif n == 40:
        proof_request = rec("xp_b_evidence_requests", original["evidence_request_id"], True)
        links = requests("issue_scoped_bank_proof_link")
        policies = requests("set_bank_proof_access_scope")
        revokes = requests("revoke_delivered_bank_proof_link")
        valid_links = bool(links)
        for link in links:
            previous = [q for q in policies if q["executed_seq"] < link["executed_seq"]]
            policy = previous[-1]["arguments"].get("choice") if previous else None
            readers = original["access_policies"].get(policy, [])
            evidence = rec("xp_b_bank_evidence", link["arguments"].get("choice"))
            valid_links &= (
                readers == [original["verified_contact"]]
                and link.get("receipt", {}).get("authorized_readers") == readers
                and (evidence.get("request_id") == original["evidence_request_id"])
                and evidence.get("delivered")
                and evidence.get("sealed")
                and (evidence.get("kind") == proof_request["required_kind"])
                and (set(original["_required_fields"]) <= set(evidence.get("field_names", [])))
                and (
                    set(evidence.get("field_names", []))
                    <= set(contract["acceptance"]["allowed_fields"])
                )
                and (set(evidence.get("transaction_ids", [])) == {proof_request["transaction_id"]})
            )
        valid = (
            bool(events("b_enhance_proof_recipient"))
            and valid_links
            and bool(events("b_enhance_proof_delivery"))
            and r.get("recipient_read")
            and (r.get("reader") == original["verified_contact"])
            and (r.get("link_active") is False)
            and (r.get("link_readers") == [original["verified_contact"]])
            and (r.get("revoked_after_reader") == original["verified_contact"])
            and bool(revokes)
        )
        add_result(
            valid,
            {
                "link_receipts": [q.get("receipt") for q in links],
                "verified_fields": original["_required_fields"],
                "reader": r.get("reader"),
                "link_active": r.get("link_active"),
                "historical_link_scopes_valid": valid_links,
            },
            "Accept according to executed permissions and immutable proof scope at the time the temporary link was actually issued; Having been opened to the team or public cannot be hidden by later narrowing, Future access terminates only after the true recipient reads it. ",
        )
    elif n == 42:
        transfer = rec("xp_b_security_transfers", original["legitimate_transfer_id"])
        original_transfer = rec("xp_b_security_transfers", original["legitimate_transfer_id"], True)
        callbacks = requests("deliver_supplier_payment_callback")
        valid_callbacks = bool(callbacks) and all(
            (
                q.get("receipt", {}).get("callback_payment_id")
                == original["legitimate_transfer_id"]
                and q.get("receipt", {}).get("callback_recipient")
                == original_transfer.get("purpose")
                for q in callbacks
            )
        )
        valid = (
            r.get("standing_status") == "cancelled"
            and cash("independent_standing_payment") == 0
            and (r.get("standing_paid_minor", 0) == 0)
            and bool(events("b_enhance_callback_audit"))
            and r.get("old_callback_revoked")
            and (r.get("callback_version", 0) > original["callback_version"])
            and (transfer.get("status") == "executed")
            and r.get("supplier_callback_delivered")
            and valid_callbacks
            and (r.get("callback_payment_id") == original["legitimate_transfer_id"])
        )
        add_result(
            valid,
            {
                "standing_status": r.get("standing_status"),
                "standing_debit_minor": -cash("independent_standing_payment"),
                "legitimate_payment": transfer.get("id"),
                "callback_receipts_valid": valid_callbacks,
            },
            "Independent bank standing instructions must stop future authorizations and produce no new deductions; Lawful expedited payment is still completed, Every actually sent callback must be bound to that payment and original supplier purpose, Cannot verify only the last callback. ",
        )
