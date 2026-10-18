from payment.runtime.evaluation.psp.accounts.observations import initial_record

TABLE = "xp_b_reasoning_recovery"
ADV = "xp_b_reasoning_accepted_advances"
DOCS = ["PX-BR-RECOVERY-ADVANCE", "PX-BR-RECOVERY-INSTRUCTIONS", "PX-BR-RECOVERY-MANDATES"]


def verify(w, c, base):
    original = initial_record(c, TABLE)
    cid = original["customer_id"]
    rows = lambda table: [x for x in w.get(table, {}).values() if x.get("customer_id") == cid]
    checks = [x for x in base["checks"] if x["name"] not in ("B-41-cash_amount", "B-41-budget")]
    obs = w["xp_b_secure_obligations"]
    o = obs[original["obligation_id"]]
    advance = rows(ADV)
    settled = [a for a in advance if a["status"] == "settled"]
    initial = c["initial_world"]
    h = rows("xp_b_recovery_handoffs")[0]

    def add(n, ok, detail, why):
        checks.append(
            dict(
                name="BR-41-" + n,
                passed=bool(ok),
                detail=detail,
                explanation=dict(check_zh=why, rule_documents=DOCS),
            )
        )

    net_cost = -sum(
        (j["amount_minor"] for j in rows("xp_journal") if j.get("record_type") == "cash_posting")
    )
    add(
        "budget",
        net_cost <= c["acceptance"]["budget_minor"],
        dict(net_cash_cost=net_cost),
        "Reconcile budget based on true net cash debits; Release of unsettled reserves cannot be counted again as actual expenses",
    )
    received = sum((a.get("received_minor", 0) for a in settled))
    early = sum(
        (
            a.get("received_minor", 0)
            for a in settled
            if a.get("received_minute", 999) <= original["advance_deadline"]
        )
    )
    paid = sum(
        (
            -j["amount_minor"]
            for j in rows("xp_journal")
            if j.get("kind") == "authenticated_payment"
            and j.get("record_type") == "cash_posting"
            and (j.get("reference_id") == o["id"])
        )
    )
    advance_cash = -sum(
        (
            j["amount_minor"]
            for j in rows("xp_journal")
            if j.get("record_type") == "cash_posting"
            and j.get("kind")
            in ("accepted_supplier_advance_reserve", "supplier_advance_reservation_release")
        )
    )
    fees = sum((initial[ADV][a["id"]]["fee_minor"] for a in settled))
    add(
        "invoice",
        early >= original["advance_minimum_minor"]
        and o.get("paid")
        and (o.get("paid_minute", 999) <= original["final_deadline"])
        and (received + paid == original["original_face_minor"])
        and (o["amount_minor"] == original["original_face_minor"] - received)
        and all((a.get("applied") for a in settled))
        and (advance_cash == received + fees),
        dict(early=early, advance=received, balance_cash=paid, advance_cash=advance_cash),
        "The same original invoice must satisfy both actual receipt obligations of prepayment and final payment, Prepaid fees are not principal, Remaining balance must not be paid twice",
    )
    desk = h.get("execution_desk_snapshot", {})
    add(
        "executor_limit",
        o.get("paid") and desk.get("maximum_invoice_balance_minor", -1) >= paid,
        dict(paid=paid, desk=desk),
        "The enterprise amount authority of the actual executing seat must cover the executed remaining balance, Bank acceptance cannot substitute for enterprise authorization",
    )
    add(
        "advance_truth",
        all(
            (
                a["received_minor"] == initial[ADV][a["id"]]["_settlement_minor"]
                and a["received_minute"]
                == a["released_minute"] + initial[ADV][a["id"]]["delay_minutes"]
                and (a["reserved_minor"] == 0)
                for a in settled
            )
        ),
        {},
        "Actual prepayment comes from asynchronous settlement of accepted instructions, And release unsettled reserved principal",
    )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
