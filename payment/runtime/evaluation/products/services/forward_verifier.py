"""Reconstruct physical currencies, contractual obligations and held margin.

No execution or reference-policy functions are imported.
"""

from copy import deepcopy


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        w = world.get("_closure_state") or world
        initial = c["initial_world"]
        cid = c["case_id"]
        r = {
            v["reference"]: deepcopy(v)
            for v in initial["service_records"].values()
            if v.get("case_id") == cid
        }
        old = deepcopy(r)
        ids = {v["id"]: k for k, v in r.items()}
        start = initial["service_cases"][cid]
        money = dict(
            USD=start["cash_minor"],
            EUR=start["state"]["eur_minor"],
            GBP=start["state"]["gbp_minor"],
        )
        fees = 0
        valid = True
        dates = True
        reserves = True
        payments = []
        books = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )

        def release(z, total):
            target = (
                z["margin_original_minor"]
                - total * z["margin_original_minor"] // z["notional_minor"]
            )
            n = z["margin_minor"] - target
            z["margin_minor"] = target
            return n

        for b in books:
            delta = dict(USD=0, EUR=0, GBP=0)
            out = {}
            m = b["minute"]
            fee = 0
            if b.get("contract") == "contingent_forwards":
                d = b["arguments"]
                a = b["kind"]
                z = r[ids[d["record_id"]]] if "record_id" in d else None
                if a == "exercise":
                    n = d["amount_minor"]
                    valid &= (
                        z["reference"] == "FLEX"
                        and type(n) is int
                        and n > 0
                        and n % z["lot_minor"] == 0
                        and m <= z["exercise_through"]
                    )
                    valid &= z["delivered_minor"] + z["closed_minor"] + n <= z["notional_minor"]
                    rel = release(z, z["delivered_minor"] + z["closed_minor"] + n)
                    cost = -(-n * z["rate_numerator"] // z["rate_denominator"])
                    z["delivered_minor"] += n
                    delta.update(USD=rel - cost, EUR=n)
                    out = dict(
                        delivered_eur_minor=n, usd_cost_minor=cost, margin_released_minor=rel
                    )
                elif a == "nominate":
                    n = d["amount_minor"]
                    valid &= (
                        z["reference"] == "STERLING"
                        and type(n) is int
                        and n > 0
                        and n % z["lot_minor"] == 0
                        and m <= z["exercise_through"]
                    )
                    valid &= z["nominated_minor"] + z["closed_minor"] + n <= z["notional_minor"]
                    z["nominated_minor"] += n
                    delta["EUR"] = -n
                    out = dict(nominated_eur_minor=n)
                elif a == "close_remainder":
                    valid &= z["status"] not in ("IN_FLIGHT", "CONFIRMED_UNPAID")
                    n = (
                        z["notional_minor"]
                        - z.get("nominated_minor", z["delivered_minor"])
                        - z["closed_minor"]
                    )
                    valid &= n > 0
                    z["closed_minor"] += n
                    rel = release(z, z["closed_minor"] + z["delivered_minor"])
                    if z["reference"] == "PENDING":
                        fee = -(-n * z["close_fee_bp"] // 10000)
                    elif z["reference"] == "STERLING":
                        fee = z["close_fee_minor"] - z["close_fee_paid_minor"]
                        z["close_fee_paid_minor"] += fee
                    delta["USD"] = rel - fee
                    out = dict(closed_notional_minor=n, margin_released_minor=rel, fee_minor=fee)
                elif a == "settle_confirmed":
                    valid &= z["reference"] == "PENDING" and z["status"] == "CONFIRMED_UNPAID"
                    n = z["confirmed_delivery_minor"]
                    rel = release(z, n + z["closed_minor"])
                    z.update(delivered_minor=n, status="PARTIALLY_SETTLED")
                    delta.update(
                        USD=rel - (-(-n * z["rate_numerator"] // z["rate_denominator"])), EUR=n
                    )
                    out = dict(actual_eur_minor=n, margin_released_minor=rel, posted=True)
                elif a == "spot":
                    n = d["amount_minor"]
                    ccy = d["currency"]
                    side = d["side"]
                    valid &= (
                        type(n) is int
                        and n > 0
                        and ccy in ("EUR", "GBP")
                        and side in ("BUY", "SELL")
                    )
                    q = r["PRICES"]
                    prefix = ccy.lower() + ("_bid" if side == "SELL" else "_ask")
                    if ccy == "EUR" and side == "BUY":
                        prefix = (
                            "eur_" + ("early" if m <= q["eur_early_through"] else "late") + "_ask"
                        )
                    num = q[prefix + "_numerator"]
                    den = q[prefix + "_denominator"]
                    usd = -(-n * num // den) if side == "BUY" else n * num // den
                    sign = 1 if side == "BUY" else -1
                    delta.update(USD=-sign * usd)
                    delta[ccy] = sign * n
                    out = dict(usd_minor=usd, rate_numerator=num, rate_denominator=den)
                elif a == "pay_invoice":
                    n = d["amount_minor"]
                    valid &= z["kind"] == "treasury_invoice" and type(n) is int and n > 0
                    dates &= m <= z["due_minute"]
                    z["paid_minor"] += n
                    delta[z["currency"]] = -n
                    payments.append(dict(invoice=z["reference"], minute=m, amount_minor=n))
                    out = dict(currency=z["currency"], paid_minor=n)
                else:
                    valid = False
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "euro_confirmation",
                "sterling_settlement",
            ):
                ev = w["events"][b["event_id"]]
                p = initial["_event_payloads"][b["event_id"]]
                valid &= ev["status"] == "COMPLETED" and ev["due_minute"] == m
                z = r[p["reference"]]
                if p["event_kind"] == "euro_confirmation":
                    n = old[z["reference"]]["_actual_delivery_minor"]
                    valid &= z["status"] == "IN_FLIGHT"
                    z["confirmed_delivery_minor"] = n
                    rel = n * z["margin_original_minor"] // z["notional_minor"]
                    posted = money["USD"] + rel >= -(
                        -n * z["rate_numerator"] // z["rate_denominator"]
                    )
                    if posted:
                        rel = release(z, n + z["closed_minor"])
                        z.update(delivered_minor=n, status="PARTIALLY_SETTLED")
                        delta.update(
                            USD=rel - (-(-n * z["rate_numerator"] // z["rate_denominator"])), EUR=n
                        )
                    else:
                        z["status"] = "CONFIRMED_UNPAID"
                        rel = 0
                    out = dict(actual_eur_minor=n, margin_released_minor=rel, posted=posted)
                else:
                    n = z["nominated_minor"]
                    gbp = n * z["rate_numerator"] // z["rate_denominator"]
                    rel = release(z, n + z["closed_minor"])
                    z.update(delivered_minor=n, status="SETTLED")
                    delta.update(USD=rel, GBP=gbp)
                    out = dict(
                        delivered_gbp_minor=gbp, settled_eur_minor=n, margin_released_minor=rel
                    )
            else:
                valid = False
            fees += fee
            for k, v in delta.items():
                money[k] += v
            valid &= (
                min(money.values()) >= 0
                and b["wallet_delta_minor"] == delta
                and b["wallet_after_minor"] == money
                and b["receipt"] == out
            )
            reserves &= money["USD"] >= c.get("reserve_minor", 4000)
        invoices = [z for z in r.values() if z["kind"] == "treasury_invoice"]
        contracts = [z for z in r.values() if z["kind"] == "currency_forward"]
        ck(
            "payments.exact_and_on_time",
            dates and all(z["paid_minor"] == z["face_minor"] for z in invoices),
            "Reconcile by currency the true commercial balance including historical payments and actual payment timing. ",
            payments=payments,
        )
        ck(
            "contracts.no_remaining_exposure",
            all(
                z["delivered_minor"] + z["closed_minor"] == z["notional_minor"]
                and z["margin_minor"] == 0
                for z in contracts
            )
            and r["PENDING"]["delivered_minor"] == r["PENDING"]["_actual_delivery_minor"]
            and r["STERLING"]["delivered_minor"] == r["STERLING"]["nominated_minor"],
            "Accepted delivery has actually been completed, All unused contract balances are genuinely closed, deposit released according to actual performance. ",
        )
        case = w["service_cases"][cid]
        ck(
            "ledger.currencies_and_margin",
            valid
            and money
            == dict(
                USD=case["cash_minor"],
                EUR=case["state"]["eur_minor"],
                GBP=case["state"]["gbp_minor"],
            )
            and fees == case["fees_minor"],
            "Independently recalculate the three wallets, Proportional margin per occurrence, Upward debit and downward credit. ",
            expected=money,
            actual_fee_minor=fees,
        )
        ck(
            "cash.reserve_and_no_foreign_surplus",
            reserves and money["EUR"] == money["GBP"] == 0,
            "The actual process retains the customer’s USD reserve, No unprocessed foreign currency balance remains after payment. ",
            reserve_minor=c.get("reserve_minor", 4000),
            final=money,
        )
        now = {v["reference"]: v for v in w["service_records"].values() if v.get("case_id") == cid}
        frame = now == r
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(rid) == v
                    for rid, v in values.items()
                    if isinstance(v, dict) and v.get("scope") != c["scope"]
                )
        ck(
            "frame.history_and_contracts",
            frame,
            "Preserve historical delivery, Contract parameters and other customer records., Only accept changes that can be reconstructed from actual events.. ",
        )
        ck(
            "events.actual_settlements",
            all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"]
            ),
            "Verify completion of actual delivery event, Do not treat notional amount as received. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "close the case only once after business completion. ",
        )
        ck(
            "process.owner_approval",
            all(
                q["approval_seq"] < q["executed_seq"]
                and any(
                    a["tool"] == "decide_request"
                    and a["arguments"]["request_id"] == q["id"]
                    and a["arguments"]["decision"] == "APPROVE"
                    and a["result"]["ok"]
                    for a in world["audit"]
                )
                for q in w["requests"].values()
                if q.get("scope") == c["scope"] and q["status"] == "EXECUTED"
            ),
            "Each actual operation has user approval bound to the request. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck(
            "state.well_formed",
            False,
            "Lacks required state for independent accounting. ",
            error=str(e),
        )
    failed = [z["name"] for z in checks if not z["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
