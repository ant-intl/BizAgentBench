"""Reconstruct gross debits, fee-in-gross deductions, net receipts and directions."""

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
        fees = 0
        state = {}
        valid = True
        floor = True
        directions = True
        timely = True
        events = set()
        for b in bookings(w, c):
            before = cash
            fee = 0
            out = {}
            m = b["minute"]
            if b.get("contract") == "contingent_net":
                d = b["arguments"]
                z = rs[ids[d["invoice_id"]]]
                ben = rs[ids[d["beneficiary_id"]]]
                valid &= z["kind"] == "net_invoice" and ben["kind"] == "net_beneficiary"
                directions &= ben["reference"] == z["current_beneficiary_ref"]
                timely &= m <= z["due_minute"]
                if b["kind"] == "send":
                    n = d["amount_minor"]
                    route = d["route"]
                    valid &= type(n) is int and n > 0
                    if route == "protected":
                        g = rs["GUARANTEE"]
                        valid &= (
                            g["used_for"] is None
                            and m <= g["available_through"]
                            and n <= g["maximum_net_minor"]
                        )
                        fee = g["fee_minor"]
                        cash -= n + fee
                        g["used_for"] = z["reference"]
                        z["received_minor"] += n
                        out = dict(
                            invoice_ref=z["reference"],
                            beneficiary_ref=ben["reference"],
                            net_minor=n,
                            route=route,
                        )
                    elif route == "standard":
                        s = state.setdefault("standard", {})
                        valid &= (
                            m < rs["ROUTES"]["standard_result_minute"]
                            and z["reference"] not in s
                            and n > max(z["possible_standard_fees_minor"])
                        )
                        cash -= n
                        s[z["reference"]] = dict(
                            gross_minor=n,
                            beneficiary_ref=ben["reference"],
                            status="SENT",
                            actual_fee_minor=None,
                            actual_net_minor=None,
                        )
                        out = dict(
                            invoice_ref=z["reference"],
                            beneficiary_ref=ben["reference"],
                            gross_minor=n,
                            route=route,
                        )
                    else:
                        valid = False
                elif b["kind"] == "topup":
                    n = d["net_minor"]
                    s = state.get("standard", {}).get(z["reference"])
                    valid &= type(n) is int and n > 0 and s is not None and s["status"] == "FINAL"
                    fee = rs["ROUTES"]["topup_fee_minor"]
                    cash -= n + fee
                    z["received_minor"] += n
                    out = dict(
                        invoice_ref=z["reference"], beneficiary_ref=ben["reference"], net_minor=n
                    )
                else:
                    valid = False
                valid &= b["receipt"] == out
            elif b["kind"] == "bank_event":
                eid = b["event_id"]
                e = ini["events"][eid]
                p = ini["_event_payloads"][eid]
                valid &= (
                    eid not in events
                    and e["due_minute"] == m
                    and w["events"][eid]["status"] == "COMPLETED"
                    and b["event_kind"] == p["event_kind"]
                )
                events.add(eid)
                if p["event_kind"] == "old_transfer_result":
                    z = rs["OLD_TRANSFER"]
                    n = z["_credited_minor"]
                    valid &= n in z["possible_credited_minor"]
                    ret = z["gross_minor"] - n - z["return_fee_minor"]
                    z.update(status="FINAL", actual_credited_minor=n, actual_returned_minor=ret)
                    rs[z["invoice_ref"]]["received_minor"] += n
                    cash += ret
                    fee = z["return_fee_minor"]
                    d = rs["DIRECTION"]
                    valid &= d["status"] == "SIGNED" and d["effective_from"] == m
                    rs[d["invoice_ref"]]["current_beneficiary_ref"] = d["beneficiary_ref"]
                    out = dict(
                        beneficiary_ref=z["beneficiary_ref"],
                        credited_minor=n,
                        returned_minor=ret,
                        return_fee_minor=fee,
                    )
                else:
                    for ref, s in state.get("standard", {}).items():
                        z = rs[ref]
                        f = z["_standard_fee_minor"]
                        valid &= f in z["possible_standard_fees_minor"] and s["status"] == "SENT"
                        n = s["gross_minor"] - f
                        s.update(status="FINAL", actual_fee_minor=f, actual_net_minor=n)
                        z["received_minor"] += n
                        fee += f
                        timely &= m <= z["due_minute"]
                        out[ref] = dict(
                            beneficiary_ref=s["beneficiary_ref"],
                            gross_minor=s["gross_minor"],
                            fee_minor=f,
                            net_minor=n,
                        )
                valid &= b["receipt"] == out
            else:
                valid = False
            fees += fee
            floor &= cash >= rs["ROUTES"]["minimum_cash_minor"]
            valid &= (
                cash >= 0
                and b["fee_minor"] == fee
                and b["cash_delta_minor"] == cash - before
                and b["cash_after_minor"] == cash
            )
        ck(
            "net.exact_actual_delivery",
            events == set(ini["events"])
            and timely
            and all(
                z["received_minor"] == z["net_due_minor"]
                for z in rs.values()
                if z["kind"] == "net_invoice"
            ),
            "Verify historical net receipts invoice by invoice., Original remittance partially received, Subsequent actual deductions and supplementary payments., Do not treat net refund as fully available. ",
        )
        ck(
            "directions.current_recipient",
            directions,
            "Legacy acceptance retains original payee binding, New instructions use the signing bank version effective at send time. ",
        )
        ck(
            "fees.actual_total",
            fees <= 7500,
            "Actually refund deducted fees, Guarantee fee, intermediary bank actual deductions and top-up fees combined must not exceed USD 75. ",
            actual_minor=fees,
        )
        ck(
            "cash.continuous_reserve",
            floor,
            "Maintain user throughout the entire process USD 50 Cash. ",
        )
        protocol(world, c, rs, cash, fees, state, valid, checks)
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck("state.well_formed", False, "Lack of independent accounting facts.. ", error=str(e))
    return outcome(checks)
