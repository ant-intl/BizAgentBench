NUMBERS = {32}
DOCS = {
    32: ["PX-A-BUYER-COLLECTION-CALENDAR", "PX-A-RETURN-VALUATION"],
}


def records(w, t):
    return [x for x in w.get("xp_a_" + t, {}).values() if x["customer_id"] == w["_customer_id"]]


def verify(world, c, previous):
    result = previous(world, c)
    n = c["number"]
    if n not in NUMBERS or not c.get("reasoning_revision"):
        return result
    checks = result["checks"]
    initial = c["initial_world"]
    p = next((x for x in records(initial, "settlement_terms") if x["number"] == n))
    live = world["xp_a_settlement_terms"][p["id"]]

    def check(key, passed, detail, zh):
        checks.append(
            dict(
                name=f"a.{n}.reasoning_{key}",
                passed=bool(passed),
                detail=detail,
                explanation=dict(check_zh=zh, rule_documents=DOCS[n]),
            )
        )

    journal = [
        j
        for j in world.get("xp_journal", {}).values()
        if j.get("customer_id") == initial["_customer_id"]
        and j.get("record_type") == "cash_posting"
    ]
    if n == 32:
        sale = records(initial, "recall_sales")[0]
        due = sale["face_minor"] - sale["_verified_return_minor"]
        receipts = records(world, "replacement_receipts")
        first = sum(
            (
                x["amount_minor"] - x.get("refunded_minor", 0)
                for x in receipts
                if x["minute"] <= p["first_due_minute"]
            )
        )
        final = sum(
            (
                x["amount_minor"] - x.get("refunded_minor", 0)
                for x in receipts
                if x["minute"] <= p["final_due_minute"]
            )
        )
        check(
            "receipt_calendar",
            first >= min(due, p["first_amount_minor"]) and final == due,
            dict(first=first, final=final, due=due),
            "Two periods accounted based on actual net receipts, Collection and later catch-up payment do not cure early delinquency. ",
        )
        services = initial["xp_a_buyer_collection_services"]
        bookings = records(world, "buyer_collection_bookings")
        expected = sum(
            (
                (b["capacity_minor"] + services[b["service_id"]]["fee_unit_minor"] - 1)
                // services[b["service_id"]]["fee_unit_minor"]
                * services[b["service_id"]]["booking_fee_minor"]
                for b in bookings
            )
        )
        actual = -sum(
            (j["amount_minor"] for j in journal if j["kind"] == "BUYER_COLLECTION_BOOKING")
        )
        check(
            "collection_budget",
            actual == expected and expected <= p["max_fees_minor"],
            dict(actual=actual, expected=expected),
            "Independently calculate fees based on original service price and reserved principal, Unused capacity is also charged. ",
        )
    else:
        deliveries = [x for x in records(world, "cash_reservations") if x["status"] == "DELIVERED"]
        valid = True
        used = set()
        for win in p["handover_windows"]:
            matching = [
                d
                for d in deliveries
                if win["from_minute"] <= d["delivery_minute"] <= win["due_minute"]
            ]
            notes = {}
            for d in matching:
                if d["id"] in used:
                    valid = False
                used.add(d["id"])
                for value, count in d["notes"].items():
                    notes[value] = notes.get(value, 0) + count
            valid = (
                valid
                and sum((d["amount_minor"] for d in matching)) == win["amount_minor"]
                and all((notes.get(k, 0) >= v for (k, v) in win["minimum_notes"].items()))
            )
        check(
            "handover_calendar",
            valid and used == {d["id"] for d in deliveries},
            deliveries,
            "the two collections have separate acceptance windows, Principal and banknote denominations, Cannot deliver total cash at once to offset the later batch authorization time. ",
        )
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda world, contract: verify(world, contract, previous)
