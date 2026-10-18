"""Independent physical-transfer ledger in original currency, including per-wallet prefixes."""

from copy import deepcopy
from payment.runtime.products.services.recovery_support import finish_audit


def verify(world, c):
    checks = []

    def ck(n, v, z, **e):
        checks.append(dict(name=n, passed=bool(v), description_zh=z, evidence=e))

    try:
        s = world.get("_closure_state") or world
        i = c["initial_world"]
        cid = c["case_id"]
        r = {
            x["reference"]: deepcopy(x)
            for x in i["service_records"].values()
            if x.get("case_id") == cid
        }
        ids = {x["id"]: x for x in r.values()}
        case = deepcopy(i["service_cases"][cid])
        valid = True
        seen = set()
        reserves = True
        timeline = []
        books = sorted(
            (
                b
                for k, b in s["service_bookings"].items()
                if k not in i["service_bookings"] and b.get("case_id") == cid
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in books:
            minute = b["minute"]
            receipt = {}
            if b.get("contract") == "origin_currency_refunds":
                d = b["arguments"]
                n = d["amount_minor"]
                valid &= type(n) is int and n > 0
                if b["kind"] == "refund":
                    p = ids[d["purchase_id"]]
                    a = ids[d["account_id"]]
                    route = next(
                        x
                        for x in p["refund_routes"]
                        if x["account"] == a["reference"] and x["currency"] == d["currency"]
                    )
                    div, rem = divmod(n * route["numerator"], route["denominator"])
                    debit = div + bool(rem)
                    valid &= (
                        p["kind"] == "refund_purchase"
                        and a["kind"] == "refund_wallet"
                        and a["cash_minor"] >= debit
                    )
                    a["cash_minor"] -= debit
                    entry = dict(
                        currency=d["currency"],
                        credited_minor=n,
                        account_ref=a["reference"],
                        debited_minor=debit,
                        minute=minute,
                    )
                    p["new_credits"].append(entry)
                    receipt = dict(purchase_ref=p["reference"], credit=deepcopy(entry))
                elif b["kind"] == "exchange":
                    q = ids[d["quote_id"]]
                    a = r[q["from_account"]]
                    z = r[q["to_account"]]
                    m = n * q["numerator"] // q["denominator"]
                    valid &= q["kind"] == "refund_fx_quote" and a["cash_minor"] >= n and m > 0
                    a["cash_minor"] -= n
                    z["cash_minor"] += m
                    receipt = dict(quote_ref=q["reference"], debited_minor=n, credited_minor=m)
                else:
                    valid = False
            elif b["kind"] == "bank_event":
                eid = b["event_id"]
                p = i["_event_payloads"][eid]
                valid &= eid not in seen and i["events"][eid]["due_minute"] == minute
                seen.add(eid)
                out = []
                for ref in p["transfers"]:
                    t = r[ref]
                    n = t["_actual_credited_minor"]
                    q, rem = divmod(n * t["funding_numerator"], t["funding_denominator"])
                    returned = t["funded_minor"] - q - bool(rem)
                    valid &= (
                        t["status"] == "IN_FLIGHT"
                        and n in t["possible_credited_minor"]
                        and returned >= 0
                    )
                    t.update(status="FINAL", credited_minor=n, returned_minor=returned)
                    r[t["funding_account"]]["cash_minor"] += returned
                    out.append(dict(transfer_ref=ref, credited_minor=n, returned_minor=returned))
                receipt = dict(transfers=out)
            else:
                valid = False
            valid &= (
                b["receipt"] == receipt
                and b["cash_delta_minor"] == 0
                and b["cash_after_minor"] == case["cash_minor"]
                and b["fee_minor"] == 0
            )
            balances = {ref: r[ref]["cash_minor"] for ref in c["reserves"]}
            reserves &= all(balances[ref] >= minimum for ref, minimum in c["reserves"].items())
            timeline.append(dict(booking_id=b["id"], minute=minute, balances=balances))
        for p in [x for x in r.values() if x["kind"] == "refund_purchase"]:
            historic = [
                x
                for x in r.values()
                if x["kind"] == "physical_refund" and x["purchase_ref"] == p["reference"]
            ]
            paid = sum(
                x["credited_minor"]
                for x in historic
                if x["status"] in ("POSTED", "FINAL") and x["currency"] == p["charge_currency"]
            ) + sum(
                x["credited_minor"]
                for x in p["new_credits"]
                if x["currency"] == p["charge_currency"]
            )
            ck(
                "refund.original_currency_exact." + p["reference"],
                paid == p["charge_minor"]
                and all(
                    x["currency"] == p["charge_currency"] and x["minute"] <= p["due_minute"]
                    for x in p["new_credits"]
                )
                and all(x["status"] in ("POSTED", "FINAL") for x in historic),
                "Reconcile customer original-currency refunds against actual receipt of the unique physical payment, Over-refund is prohibited, Wrong currency, Omitted refund or overdue. ",
                currency=p["charge_currency"],
                credited_minor=paid,
                owed_minor=p["charge_minor"],
            )
        ck(
            "cash.each_currency_reserve",
            reserves,
            "After each refund and exchange, separately verify the minimum retained amounts for both currencies, do not treat uncredited funds or historical exchange rates as currently available funds. ",
            timeline=timeline,
        )
        return finish_audit(world, c, r, case, checks, valid, seen)
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        ck(
            "state.well_formed",
            False,
            "Original currency, Original funds or actual receipt reconciliation records are missing. ",
            error=str(exc),
        )
        return dict(
            success=False,
            failed=[x["name"] for x in checks if not x["passed"]],
            checks=checks,
            allowed_terminals=["COMPLETED"],
        )
