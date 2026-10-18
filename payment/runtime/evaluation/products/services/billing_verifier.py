"""Independent finite price comparison and event-ledger reconstruction."""

from itertools import product


def minimum_options(rows, eligible=True):
    plans = {k: r for k, r in rows.items() if r["kind"] == "subscription_plan"}
    periods = sorted(
        (r for r in rows.values() if r["kind"] == "billing_period"),
        key=lambda r: r["billing_minute"],
    )
    v = rows["VOUCHER"]
    out = []
    for refs in product(sorted(plans), repeat=2):
        for redemption in ([-1, 0, 1] if v["_owned"] and eligible else [-1]):
            charges = []
            for i, (ref, period) in enumerate(zip(refs, periods)):
                p = plans[ref]
                usage = period["_actual_usage"]
                n = sum(sum(x.values()) for x in usage.values())
                variable = sum(
                    x[cat] * rate for x in usage.values() for cat, rate in p["rates_minor"].items()
                )
                credit = (
                    p["usage_credit_minor"]
                    if eligible and n >= p["credit_minimum_transactions"]
                    else 0
                )
                charges.append(
                    p["fixed_minor"] - min(p["fixed_minor"], v["cap_minor"])
                    if i == redemption
                    else p["fixed_minor"]
                )
                charges[-1] += max(0, variable - credit)
            out.append(
                dict(
                    total_minor=sum(charges),
                    plans=list(refs),
                    redemption=redemption,
                    charges_minor=charges,
                )
            )
    return sorted(out, key=lambda x: (x["total_minor"], str(x)))


def verify(world, c):
    checks = []

    def check(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        initial = c["initial_world"]
        w = world.get("_closure_state") or world
        cid = c["case_id"]
        old = {
            r["reference"]: r
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        now = {r["reference"]: r for r in w["service_records"].values() if r.get("case_id") == cid}
        ids = {r["id"]: k for k, r in old.items()}
        terms = old["SOURCE"]["_response"]
        cash = initial["service_cases"][cid]["cash_minor"]
        fees = 0
        valid = True
        reserve = True
        accounts = {k: r["status"] for k, r in old.items() if r["kind"] == "billing_account"}
        closed = {k: r["closed_minute"] for k, r in old.items() if r["kind"] == "billing_account"}
        elections = {k: r["plan"] for k, r in old.items() if r["kind"] == "billing_period"}
        billed = {}
        voucher = dict(status=old["VOUCHER"]["status"], reserved_period=None, consumed_period=None)
        v = old["VOUCHER"]

        def qualifies(age):
            return any(
                accounts[k] == "ACTIVE"
                and r["group"] == v["group"]
                and r["partner_age_days"] >= age
                for k, r in old.items()
                if r["kind"] == "billing_account"
            )

        bookings = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in bookings:
            delta = 0
            minute = b["minute"]
            if b.get("contract") == "prospective_billing":
                d = b["arguments"]
                ref = ids[d["record_id"]]
                action = b["kind"]
                if action == "select_plan":
                    valid &= ref not in billed and minute < old[ref]["billing_minute"]
                    elections[ref] = ids[d["plan_id"]]
                elif action == "reserve_voucher":
                    valid &= (
                        v["_owned"]
                        and d["code"] == v["_valid_code"]
                        and ref not in billed
                        and minute < old[ref]["billing_minute"]
                        and voucher["status"] in ("AVAILABLE", "RESERVED")
                        and qualifies(v["partner_age_days"])
                    )
                    voucher.update(status="RESERVED", reserved_period=ref)
                elif action == "close_account":
                    valid &= accounts[ref] == "ACTIVE"
                    accounts[ref] = "CLOSED"
                    closed[ref] = minute
                    if voucher["status"] in ("AVAILABLE", "RESERVED") and not qualifies(
                        v["partner_age_days"]
                    ):
                        voucher.update(status="VOID", reserved_period=None)
                else:
                    valid = False
            elif b["kind"] == "bank_event" and b.get("event_kind") == "subscription_billing":
                eid = b["event_id"]
                event = w["events"][eid]
                ref = w["_event_payloads"][eid]["reference"]
                r = old[ref]
                p = old[elections[ref]]
                u = r["_actual_usage"]
                valid &= (
                    ref not in billed
                    and minute == r["billing_minute"] == event["due_minute"]
                    and event["status"] == "COMPLETED"
                )
                count = sum(sum(a.values()) for a in u.values())
                var = sum(a[k] * p["rates_minor"][k] for a in u.values() for k in a)
                credit = (
                    min(var, p["usage_credit_minor"])
                    if count >= p["credit_minimum_transactions"]
                    and qualifies(p["credit_partner_age_days"])
                    else 0
                )
                discount = (
                    min(v["cap_minor"], p["fixed_minor"])
                    if voucher["status"] == "RESERVED"
                    and voucher["reserved_period"] == ref
                    and qualifies(v["partner_age_days"])
                    else 0
                )
                charge = p["fixed_minor"] + var - credit - discount
                paid = cash >= charge
                receipt = dict(
                    reference=ref,
                    plan=elections[ref],
                    actual_usage=u,
                    fixed_minor=p["fixed_minor"],
                    variable_minor=var,
                    usage_credit_minor=credit,
                    coupon_discount_minor=discount,
                    charge_minor=charge,
                    paid=paid,
                )
                valid &= b["receipt"] == receipt
                billed[ref] = receipt
                if paid:
                    delta = -charge
                    fees += charge
                    if voucher["status"] == "RESERVED" and voucher["reserved_period"] == ref:
                        voucher.update(status="CONSUMED", consumed_period=ref)
                valid &= (
                    now[ref]["status"] == ("PAID" if paid else "UNPAID")
                    and now[ref]["charge_minor"] == charge
                    and now[ref]["actual_usage"] == u
                    and now[ref]["coupon_discount_minor"] == discount
                    and now[ref]["usage_credit_applied_minor"] == credit
                    and now[ref]["billed_minute"] == minute
                )
            else:
                continue
            cash += delta
            reserve &= cash >= terms["minimum_cash_reserve_minor"]
            valid &= b["cash_delta_minor"] == delta and b["cash_after_minor"] == cash
        choices = minimum_options(old)
        minimum = choices[0]["total_minor"]
        check(
            "billing.minimum_total_cost",
            fees == minimum
            and set(billed) == set(elections)
            and all(b["paid"] for b in billed.values()),
            "Independently enumerate two-period packages and one-time discount allocations, Compare total future actual costs, Exclude historical sunk costs. ",
            actual_minor=fees,
            minimum_minor=minimum,
            search_space=len(choices),
            optimal_options=[p for p in choices if p["total_minor"] == minimum],
        )
        check(
            "billing.actual_components",
            valid
            and all(now[k]["plan"] == v for k, v in elections.items())
            and all(now["VOUCHER"][k] == v for k, v in voucher.items()),
            "Recalculate group usage from the actual execution time, Shared eligibility, Deductions and discounted spending, Verify the complete bill and coupon status. ",
        )
        retired = terms["retired_account"]
        check(
            "accounts.requested_retirement",
            accounts[retired] == "CLOSED"
            and closed[retired] <= terms["close_by_minute"]
            and all(accounts[k] == "ACTIVE" for k in accounts if k != retired),
            "Close the old account within the agreed period, Retain both operating accounts simultaneously. ",
        )
        check(
            "cash.independent_balance",
            cash == w["service_cases"][cid]["cash_minor"]
            and fees == w["service_cases"][cid]["fees_minor"]
            and reserve,
            "Verify actual fees for this round, Cash and continuous reserve. ",
            expected_cash_minor=cash,
        )
        check(
            "events.completed",
            w["minute"] >= terms["final_minute"]
            and all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"]
            ),
            "Close after actual completion of two billing periods. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        check(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close case only once after actual result is completed. ",
        )
        check(
            "process.approval",
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
            "Each operation has user approval with matching parameters. ",
        )
        mutable = {
            "billing_account": {"status", "closed_minute"},
            "billing_period": {
                "plan",
                "status",
                "actual_usage",
                "charge_minor",
                "coupon_discount_minor",
                "usage_credit_applied_minor",
                "billed_minute",
            },
            "partner_voucher": {"status", "reserved_period", "consumed_period"},
            "external_correspondence": {"receipt", "receipt_status"},
        }
        frame = set(old) == set(now) and all(
            all(now[k].get(f) == v for f, v in r.items() if f not in mutable.get(r["kind"], set()))
            for k, r in old.items()
        )
        frame &= all(
            now[k]["status"] == v and now[k]["closed_minute"] == closed[k]
            for k, v in accounts.items()
        )
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(rid) == r
                    for rid, r in values.items()
                    if isinstance(r, dict) and r.get("scope") != c["scope"]
                )
        check(
            "frame.records_and_history",
            frame,
            "Retain original pricing, Historical bills., Group affiliation and other customer information. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        check(
            "state.well_formed",
            False,
            "Missing fields required for independent accounting. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
