from payment.runtime.evaluation.psp.receivables import collection_and_cash as core


def verify(world, contract, check):
    n = contract["number"]
    r = core.records
    initial = contract["initial_world"]
    if n == 32:
        original = r(initial, "recall_sales")[0]
        sale = r(world, "recall_sales")[0]
        notes = r(world, "sale_credit_notes")
        receipts = r(world, "replacement_receipts")
        authorized = original["_verified_return_minor"]
        due = original["face_minor"] - authorized
        check(
            "commercial_credit",
            sum((x["amount_minor"] for x in notes)) == authorized
            and sale["credit_minor"] == authorized,
            notes,
            "Commercial credit amount equals independent return review amount, Bank recall cannot be used to extinguish claim for unreturned goods. ",
        )
        check(
            "replacement_cash",
            sum((x["amount_minor"] - x.get("refunded_minor", 0) for x in receipts)) == due
            and sale.get("replacement_received_minor", 0)
            == sum((x["amount_minor"] for x in receipts)),
            receipts,
            "calculate net recovery as actual credit of substitute payment minus refunds already received by original buyer, Allow collecting full payment first and refunding overcharged portion afterward. ",
        )
        check(
            "commercial_application",
            sale.get("paid_minor", 0) == due
            and sale.get("credit_minor", 0) + sale.get("paid_minor", 0) == original["face_minor"]
            and all((x.get("applied") for x in receipts)),
            sale,
            "Cash received and write-off are reconciled separately.; applied payments and genuine credit amount fully cover the original invoice. ",
        )
    else:
        deliveries = [x for x in r(world, "cash_reservations") if x["status"] == "DELIVERED"]
        notes = {}
        for d in deliveries:
            for value, count in d.get("notes", {}).items():
                notes[value] = notes.get(value, 0) + count
        required = contract["acceptance"]["required_notes"]
        check(
            "cash_denominations",
            all((notes.get(k, 0) >= v for (k, v) in required.items()))
            and all(
                (
                    sum((int(k) * v for (k, v) in d.get("notes", {}).items())) == d["amount_minor"]
                    for d in deliveries
                )
            ),
            notes,
            "Cash delivery must meet customer’s actual denomination requirements., Correct amount is insufficient to prove correct banknote combination.. ",
        )
        authority = r(initial, "cash_authorities")[0]
        check(
            "pickup_authority",
            all((d["amount_minor"] <= authority["max_pickup_minor"] for d in deliveries)),
            deliveries,
            "Each cash delivery must not exceed original valid authorization limit.. ",
        )
        stocks = {a["id"]: dict(a["_counted_notes"]) for a in r(initial, "cash_agents")}
        used = {a: {} for a in stocks}
        for reservation in r(world, "cash_reservations"):
            for value, count in reservation.get("notes", {}).items():
                used[reservation["agent_id"]][value] = (
                    used[reservation["agent_id"]].get(value, 0) + count
                )
        ok = all(
            (
                all((count <= stocks[a].get(value, 0) for (value, count) in counts.items()))
                for (a, counts) in used.items()
            )
        )
        check(
            "physical_stock",
            ok,
            used,
            "Independently reconcile cumulative banknote occupation based on the original bank’s actual inventory, A single count cannot repeatedly generate cash. ",
        )
