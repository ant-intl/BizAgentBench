SUPPORTED = {48, 50}
TABLE = {
    48: "debtor_tranches",
    50: "wallet_payrolls",
}
ZH = {
    48: "Complete the two supplier commitments after the genuine receivable installments are received.; Charge the rescheduling fee, Non-recourse residual financing and non-delayable critical payments are calculated jointly. ",
    50: "Fix wallet feedback while completing procurement and actual payroll batch, Arrange top-up and payment according to the salary amount published after purchase, And verify the stability window of the final rule. ",
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
    j = [
        x
        for x in w.get("xp_journal", {}).values()
        if x.get("customer_id") == owner
        and x.get("record_type") == "cash_posting"
        and (x["id"] not in initial.get("xp_journal", {}))
    ]

    def money(k):
        return sum((x["amount_minor"] for x in j if x["kind"] == k))

    def ck(key, ok, detail):
        checks.append(
            dict(
                name=f"c{n}_reasoning_{key}",
                passed=bool(ok),
                detail=detail,
                explanation={"check_zh": ZH[n], "rule_documents": [f"PX-C-R{n}", f"PX-C-{n}"]},
            )
        )

    if n == 48:
        checks = [x for x in checks if x["name"] != "c48_linked_accounting"]
        claim = initial["xp_c_receivable_disputes"][a["linked_contract_id"]]["_bank"][
            "undisputed_minor"
        ]
        first = money("committed_receivable")
        later = money("later_debtor_collection")
        assigned = r.get("assigned_face_minor", 0)
        remaining = r.get("remaining_minor", claim)
        ck(
            "rights",
            first + later + assigned + remaining == claim
            and first == r0["_first_minor"]
            and (
                not assigned
                or (
                    money("residual_factor_advance") + r.get("factor_fee_minor", 0) == assigned
                    and (not later)
                )
            ),
            dict(first=first, later=later, assigned=assigned, remaining=remaining),
        )
        ck(
            "fees",
            -money("plan_negotiation_fee") + r.get("factor_fee_minor", 0)
            <= r0["finance_fee_cap_minor"],
            -money("plan_negotiation_fee") + r.get("factor_fee_minor", 0),
        )
        linked = w["xp_c_receivable_disputes"][a["linked_contract_id"]]
        ck(
            "receipt",
            linked.get("receipt_allocated_minor") == first,
            linked.get("receipt_allocated_minor"),
        )
    else:
        ck(
            "payroll",
            r.get("paid_minor") == r0["_payroll_minor"]
            and -money("wallet_staff_payroll") == r0["_payroll_minor"]
            and (r.get("paid_cycle", 10**9) <= r0["due_cycle"]),
            dict(paid=r.get("paid_minor"), cycle=r.get("paid_cycle")),
        )
        auto = sum(
            (
                x["fee_minor"]
                for x in w.get("xp_c_automation_actions", {}).values()
                if x.get("customer_id") == owner
            )
        )
        manual = -money("payroll_manual_funding") - money("payroll_manual_credit")
        ck("fees", auto + manual <= r0["total_fee_cap_minor"], dict(automatic=auto, manual=manual))
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda w, c: (
        verify(w, c, previous)
        if c["number"] in SUPPORTED and c.get("reasoning_revision")
        else previous(w, c)
    )
