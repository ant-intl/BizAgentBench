TABLES = {38: "xp_b_tax_handovers", 41: "xp_b_recovery_handoffs"}


def verify(world, c, base):
    from payment.runtime.products.psp.accounts.tax_and_authority import tax_clearance_facts

    n = c["number"]
    initial = c["initial_world"]
    rid = c["acceptance"]["revision_record_id"]
    r = world[TABLES[n]][rid]
    original = initial[TABLES[n]][rid]
    checks = list(base["checks"])

    def add(name, ok, detail, reason):
        checks.append(
            dict(
                name=f"B-{n}-{name}",
                passed=bool(ok),
                detail=detail,
                explanation=dict(check_zh=reason, rule_documents=[f"PX-B-{n}"]),
            )
        )

    if n == 38:
        receipts = list(world.get("xp_b_tax_settlement_receipts", {}).values())
        alloc = list(world.get("xp_b_tax_receipt_allocations", {}).values())
        obs = world["xp_b_tax_obligations"]
        byid = {x["id"]: x for x in receipts}
        valid = True
        summary = {}
        for oid, face in original["original_liabilities"].items():
            o = obs[oid]
            own = [x for x in alloc if x["obligation_id"] == oid]
            amount = sum((x["amount_minor"] for x in own))
            actual_old = sum(
                (x["amount_minor"] for x in receipts if x["jurisdiction"] == o["jurisdiction"])
            )
            valid &= (
                all((byid[x["receipt_id"]]["jurisdiction"] == o["jurisdiction"] for x in own))
                and o["amount_minor"] + amount == face
                and o.get("paid")
                and (o["amount_minor"] + actual_old == face)
            )
            summary[oid] = dict(
                face=face,
                allocated=amount,
                actual_predecessor=actual_old,
                new_cash=o["amount_minor"],
            )
        valid &= all(
            (
                sum((x["amount_minor"] for x in alloc if x["receipt_id"] == p["id"]))
                == p["amount_minor"]
                for p in receipts
            )
        )
        instructions = list(world["xp_b_retiring_tax_instructions"].values())
        terminal = all(
            (
                (
                    x["status"] == "settled"
                    if initial["xp_b_retiring_tax_instructions"][x["id"]]["_bank_status"]
                    == "accepted"
                    else x["status"] in ("cancelled", "settled")
                )
                for x in instructions
            )
        )
        cash = -sum(
            (
                j["amount_minor"]
                for j in world.get("xp_journal", {}).values()
                if j.get("kind") == "retiring_tax_settlement"
                and j.get("record_type") == "cash_posting"
            )
        )
        add(
            "coupling",
            valid
            and r.get("clearance_published")
            and r.get("settlement_published")
            and (cash == sum((p["amount_minor"] for p in receipts))),
            summary,
            "Recompute original tax liability by jurisdiction=Actual predecessor payment+Actual new cash; Voucher allocation can use the original payment in that jurisdiction only once",
        )
        add(
            "handover",
            terminal and (not r["active_jurisdictions"]),
            {"instructions": instructions, "active_scopes": r["active_jurisdictions"]},
            "Old instruction is withdrawn or included in original tax debt according to actual settlement, And all permissions of the departing agent are terminated; Do not force queued instructions that could lawfully settle to be canceled",
        )
        snapshot = r.get("clearance_snapshot")
        current = tax_clearance_facts(world, r)
        add(
            "clearance_snapshot",
            bool(snapshot)
            and snapshot == current
            and snapshot["census_received"]
            and snapshot["settlement_published"]
            and (not snapshot["remaining_scope"])
            and all((item["paid"] for item in snapshot["obligations"].values())),
            {"at_publication": snapshot, "current": current},
            "Handover list must be verified against actual tax and jurisdiction attribution, Issued after old permissions exit; Subsequent changes in facts require reissuance. ",
        )
    else:
        x = world.get("xp_b_recovery_instructions", {}).get(r.get("used_instruction_id"), {})
        o = world["xp_b_secure_obligations"][original["obligation_id"]]
        desk = r.get("execution_desk_snapshot", {})
        time = o.get("paid_minute", -1)
        authority = not original["handover_required"] or (
            desk.get("permission") == "execute"
            and desk.get("start", 999) <= time <= desk.get("end", -1)
            and (r.get("ready_minute", 999) <= time)
        )
        challenge = world.get("xp_b_payment_challenges", {}).get(o.get("challenge_id"), {})
        used = [
            q for q in world["xp_b_recovery_instructions"].values() if q["status"] == "executed"
        ]
        add(
            "coupling",
            len(used) == 1
            and x.get("obligation_id") == original["obligation_id"]
            and (x.get("amount_minor") == o["amount_minor"])
            and (x.get("credential_version") == o.get("credential_version"))
            and (challenge.get("instruction_id") == x.get("id"))
            and authority,
            {"instruction": x, "desk_at_payment": desk, "minute": time},
            "Original payments pending approval must be completed according to current credentials and the seat that actually has execution authority.; Login or preparation permission cannot replace execution authorization.",
        )
        add(
            "temporary_access",
            r.get("handover_closed")
            and (not r.get("executor_ready"))
            and (
                world["xp_b_access_profiles"][original["profile_id"]]["payment_status"] == "active"
            ),
            {},
            "After original payment completes, remove temporary execution seat, But retain the restored customer payment capability",
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous, *, baseline):
    return lambda w, c: (verify(w, c, baseline(w, c)) if c["number"] in TABLES else previous(w, c))
