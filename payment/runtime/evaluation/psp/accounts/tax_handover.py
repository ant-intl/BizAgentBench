from payment.runtime.evaluation.psp.accounts.observations import initial_record

TABLE = "xp_b_reasoning_tax_capacity"
BOOK = "xp_b_reasoning_tax_bookings"
PAY = "xp_b_reasoning_tax_payments"
DOCS = ["PX-BR-TAX-CAPACITY", "PX-BR-TAX-ROUTES", "PX-BR-TAX-CENSUS"]


def verify(w, c, base):
    checks = list(base["checks"])
    original = initial_record(c, TABLE)
    cid = original["customer_id"]
    rows = lambda t: [x for x in w.get(t, {}).values() if x.get("customer_id") == cid]
    ps = rows(PAY)
    bs = rows(BOOK)
    old = rows("xp_b_tax_settlement_receipts")
    h = initial_record(c, "xp_b_tax_handovers")

    def add(k, ok, d, t):
        checks.append(
            dict(
                name="BR-38-" + k,
                passed=bool(ok),
                detail=d,
                explanation=dict(check_zh=t, rule_documents=DOCS),
            )
        )

    summaries = {}
    valid = True
    for oid, face in h["original_liabilities"].items():
        o = c["initial_world"]["xp_b_tax_obligations"][oid]
        early = sum(
            (
                p["amount_minor"]
                for p in old
                if p["jurisdiction"] == o["jurisdiction"] and p["received_minute"] <= o["deadline"]
            )
        )
        current = sum(
            (
                p["received_minor"]
                for p in ps
                if p["obligation_id"] == oid and p.get("received_minute", 999) <= o["deadline"]
            )
        )
        summaries[oid] = dict(predecessor=early, successor=current, face=face)
        valid &= early + current == face
    add(
        "receipts",
        valid and all((p["status"] == "received" for p in ps)),
        summaries,
        "Per jurisdiction, reconcile the original debt and respective terms by actual receipt rather than bank submission",
    )
    capacity = sum((x["reserved_minor"] for x in bs)) <= original["total_capacity_minor"] and all(
        (0 <= x["used_minor"] <= x["reserved_minor"] for x in bs)
    )
    for j in original["jurisdictions"]:
        capacity &= sum((x["used_minor"] for x in bs if x["jurisdiction"] == j)) == sum(
            (
                p["amount_minor"]
                for p in ps
                if w["xp_b_tax_agents"][p["recipient_id"]]["kind"] == "agent"
                and w["xp_b_tax_obligations"][p["obligation_id"]]["jurisdiction"] == j
            )
        )
    add(
        "capacity",
        capacity,
        bs,
        "Actual agent principal consumption corresponds to the limited processing quota of the relevant jurisdiction; Used limit does not recover due to subsequent allocation",
    )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
