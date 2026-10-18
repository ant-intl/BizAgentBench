"""Rebuild bill-right ownership, buyer cash and physical document movements."""

from payment.runtime.products.services.collection_support import (
    records,
    bookings,
    protocol,
    outcome,
)


def verify(world, c):
    checks = []

    def ck(n, ok, zh, **kw):
        checks.append(dict(name=n, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        w = world.get("_closure_state") or world
        ini = c["initial_world"]
        rs = records(c)
        ids = {z["id"]: k for k, z in rs.items()}
        cash = ini["service_cases"][c["case_id"]]["cash_minor"]
        initial_cash = cash
        fees = 0
        valid = True
        floor = True
        docs = True
        timely = True
        events = set()
        sales = 0
        for b in bookings(w, c):
            before = cash
            fee = 0
            out = {}
            m = b["minute"]
            a = b["kind"]
            if b.get("contract") == "actual_collections":
                d = b["arguments"]
                if a == "discount":
                    z = rs[ids[d["bill_id"]]]
                    n = d["amount_minor"]
                    valid &= (
                        z["kind"] == "collection_bill"
                        and z["status"] == "ACCEPTED"
                        and not z["discount_used"]
                        and type(n) is int
                        and z["discount_fee_minor"]
                        < n
                        <= z["accepted_minor"]
                        - z["received_minor"]
                        - z["assigned_outstanding_minor"]
                    )
                    fee = z["discount_fee_minor"]
                    cash += n - fee
                    z.update(discount_used=True, discounted_minor=n, assigned_outstanding_minor=n)
                    sales += n
                    out = dict(bill_ref=z["reference"], assigned_minor=n)
                elif a == "pay":
                    z = rs[ids[d["payable_id"]]]
                    n = d["amount_minor"]
                    valid &= z["kind"] == "payable" and type(n) is int and n > 0
                    cash -= n
                    z["paid_minor"] += n
                    timely &= m <= z["due_minute"]
                    out = dict(payable_ref=z["reference"], paid_minor=n)
                elif a in ("deliver", "return_documents"):
                    z = rs[ids[d["bill_id"]]]
                    n = d["quantity_units"]
                    valid &= z["kind"] == "collection_bill" and type(n) is int and n > 0
                    key = "delivered_units" if a == "deliver" else "returned_units"
                    z[key] += n
                    valid &= (
                        z["delivered_units"] + z["returned_units"]
                        <= z["face_minor"] // z["unit_price_minor"]
                    )
                    timely &= m <= z["delivery_due_minute"]
                    if a == "deliver":
                        docs &= (
                            z["accepted_minor"] is not None
                            and z["delivered_units"] * z["unit_price_minor"] <= z["accepted_minor"]
                            and (
                                z["document_mode"] == "DA"
                                or z["delivered_units"] * z["unit_price_minor"]
                                <= z["received_minor"]
                            )
                        )
                    else:
                        docs &= (
                            z["accepted_minor"] is not None
                            and z["returned_units"] * z["unit_price_minor"]
                            <= z["face_minor"] - z["accepted_minor"]
                        )
                    out = dict(bill_ref=z["reference"], quantity_units=n)
                else:
                    valid = False
                valid &= b["receipt"] == out and b["fee_minor"] == fee
            elif a == "bank_event":
                eid = b["event_id"]
                e = ini["events"][eid]
                p = ini["_event_payloads"][eid]
                valid &= (
                    eid not in events
                    and m == e["due_minute"]
                    and w["events"][eid]["status"] == "COMPLETED"
                )
                events.add(eid)
                for z in rs.values():
                    if z["kind"] != "collection_bill":
                        continue
                    if p["event_kind"] == "acceptance":
                        n = z["_accepted_minor"]
                        valid &= n in z["possible_accepted_minor"]
                        z.update(accepted_minor=n, status="ACCEPTED")
                        out[z["reference"]] = dict(accepted_minor=n)
                    else:
                        n = (
                            rs["TERMS"]["early_receipts"][z["reference"]]
                            if p["event_kind"] == "early_receipt"
                            else z["accepted_minor"] - z["received_minor"]
                        )
                        sold = min(n, z["assigned_outstanding_minor"])
                        z["received_minor"] += n
                        z["assigned_outstanding_minor"] -= sold
                        cash += n - sold
                        out[z["reference"]] = dict(
                            buyer_paid_minor=n,
                            assigned_discharge_minor=sold,
                            cash_credit_minor=n - sold,
                        )
                valid &= b["receipt"] == out and b["event_kind"] == p["event_kind"]
            else:
                valid = False
            fees += fee
            floor &= cash >= rs["TERMS"]["minimum_cash_minor"]
            valid &= (
                cash >= 0
                and b["cash_delta_minor"] == cash - before
                and b["cash_after_minor"] == cash
            )
        choices = []
        gap = rs["SUPPLIER"]["amount_minor"] + rs["TERMS"]["minimum_cash_minor"] - initial_cash
        for z in rs.values():
            if z["kind"] == "collection_bill":
                n = gap + z["discount_fee_minor"]
                if n <= z["_accepted_minor"] - z["historical_received_minor"]:
                    choices.append((n, z["discount_fee_minor"]))
        optimum = min(choices)
        ck(
            "finance.minimum_principal_and_fee",
            (sales, fees) == optimum and fees <= 2000,
            "independently compare all single-ticket financing options for the first due payment; Mixed discount incurs at least two fee payments, Cannot reduce the minimum principal per ticket. ",
            actual=[sales, fees],
            optimum=list(optimum),
        )
        ck(
            "collections.actual_rights_and_cash",
            events == set(ini["events"])
            and all(
                z["received_minor"] == z["accepted_minor"] and z["assigned_outstanding_minor"] == 0
                for z in rs.values()
                if z["kind"] == "collection_bill"
            ),
            "Settle transferred rights for each invoice with actual buyer payment, Prohibit counting financing funds repeatedly as buyer receipts. ",
        )
        ck(
            "documents.acceptance_and_payment",
            docs
            and all(
                z["delivered_units"] * z["unit_price_minor"] == z["accepted_minor"]
                and z["returned_units"] * z["unit_price_minor"]
                == z["face_minor"] - z["accepted_minor"]
                for z in rs.values()
                if z["kind"] == "collection_bill"
            ),
            "DA Documents against acceptance, DP Present documents against actual payment., Reject goods documents and return to exporter. ",
        )
        ck(
            "payables.full_and_timely",
            timely
            and all(
                z["paid_minor"] == z["amount_minor"] for z in rs.values() if z["kind"] == "payable"
            ),
            "Supplier, Agency payments and actual document movement are completed in full as agreed. ",
        )
        ck("cash.continuous_reserve", floor, "retain customer throughout the process USD 50 Cash. ")
        protocol(world, c, rs, cash, fees, {}, valid, checks)
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck("state.well_formed", False, "Lack of independent accounting facts.. ", error=str(e))
    return outcome(checks)
