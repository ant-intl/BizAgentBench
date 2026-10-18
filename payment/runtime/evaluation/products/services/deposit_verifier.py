from copy import deepcopy
from payment.runtime.products.services.deposit_bound import maximum
from payment.runtime.products.services.temporal_support import audit_frame


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
        original = deepcopy(r)
        ids = {x["id"]: k for k, x in r.items()}
        case = deepcopy(i["service_cases"][cid])
        seen = set()
        valid = True
        reserve = True
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
            before = case["cash_minor"]
            fee = 0
            now = b["minute"]
            kind = b["kind"]
            if b.get("contract") == "tiered_deposit_calendar":
                data = b["arguments"]
                if kind == "redeem_early":
                    d = r[ids[data["deposit_id"]]]
                    valid &= (
                        d["kind"] == "dated_term_deposit"
                        and d["status"] == "ACTIVE"
                        and now < d["maturity_minute"]
                    )
                    payout = d["principal_minor"] - d["break_charge_minor"]
                    fee = d["break_charge_minor"]
                    case["cash_minor"] += payout
                    d.update(
                        status="BROKEN", break_minute=now, payout_minor=payout, payout_minute=now
                    )
                elif kind == "pay_obligation":
                    o = r[ids[data["obligation_id"]]]
                    n = data["amount_minor"]
                    valid &= o["kind"] == "deposit_obligation" and type(n) is int and n > 0
                    case["cash_minor"] -= n
                    o["paid_minor"] += n
                    o["last_paid_minute"] = now
                else:
                    valid = False
            elif kind == "bank_event":
                eid = b["event_id"]
                valid &= eid not in seen and i["events"][eid]["due_minute"] == now
                seen.add(eid)
                review = r["REIMBURSEMENT"]
                receipt = 0
                if now == review["effective_minute"]:
                    receipt = review["_actual_amount_minor"]
                    valid &= receipt in review["possible_amounts_minor"]
                    review.update(actual_amount_minor=receipt, status="FINAL")
                    case["cash_minor"] += receipt
                ds = [d for d in r.values() if d["kind"] == "dated_term_deposit"]
                tier = r["TIER"]
                eligible = (
                    sum(
                        d["principal_minor"]
                        for d in ds
                        if d["tier_group"] == tier["group"] and d["break_minute"] is None
                    )
                    >= tier["qualifying_original_principal_minor"]
                )
                if now == tier["qualification_through"]:
                    tier.update(finalized=True, qualified=eligible)
                if tier["finalized"]:
                    eligible = tier["qualified"]
                payouts = []
                for d in ds:
                    if d["status"] == "ACTIVE" and d["maturity_minute"] == now:
                        interest = d["base_interest_minor"] + (
                            d["tier_bonus_minor"] if eligible else 0
                        )
                        amount = d["principal_minor"] + interest
                        case["cash_minor"] += amount
                        d.update(
                            status="MATURED",
                            payout_minor=amount,
                            interest_minor=interest,
                            payout_minute=now,
                        )
                        payouts.append(dict(deposit_ref=d["reference"], amount_minor=amount))
                auto = r["RENT"]
                if now == auto["due_minute"] and case["cash_minor"] >= auto["amount_minor"]:
                    case["cash_minor"] -= auto["amount_minor"]
                    auto["paid"] = True
                valid &= b["receipt"] == dict(reimbursement_minor=receipt, payouts=payouts)
            else:
                valid = False
            case["fees_minor"] += fee
            valid &= (
                case["cash_minor"] >= 0
                and b["cash_delta_minor"] == case["cash_minor"] - before
                and b["cash_after_minor"] == case["cash_minor"]
                and b["fee_minor"] == fee
            )
            reserve &= case["cash_minor"] >= c["reserve_minor"]
            timeline.append(dict(booking=b["id"], minute=now, cash_minor=case["cash_minor"]))
        bills = [x for x in r.values() if x["kind"] == "deposit_obligation"]
        ck(
            "payments.all_obligations_and_actual_debit",
            all(
                x["paid_minor"] == x["amount_minor"]
                and x["last_paid_minute"] is not None
                and x["last_paid_minute"] <= x["due_minute"]
                for x in bills
            )
            and r["RENT"]["paid"],
            "Wages., Supplier, Taxes and fees, Insurance and automatic rent are both actually completed in full and on time. ",
        )
        # All deposits may be settled before the last contractual date when
        # later-maturity deposits were redeemed early. Actual status and tier
        # finalization determine completion; payment deadlines remain separate.
        ck(
            "deposits.final_maturity_and_tier",
            all(x["status"] != "ACTIVE" for x in r.values() if x["kind"] == "dated_term_deposit")
            and r["TIER"]["finalized"],
            "Calculate yield for each certificate of deposit based on actual redemption and relationship tier, Confirm all have been paid or early redeemed and the hierarchy is finalized. ",
        )
        bound = maximum(
            original,
            i["service_cases"][cid]["cash_minor"],
            c["reserve_minor"],
            r["REIMBURSEMENT"]["_actual_amount_minor"],
        )
        ck(
            "cash.maximum_complete_retention",
            bound["feasible"] and case["cash_minor"] == bound["maximum_final_cash_minor"],
            "Independently traverse all early redemption dates, Reconcile the maximum final cash that can complete all obligations. ",
            certificate=bound,
            actual=case["cash_minor"],
        )
        ck(
            "cash.continuous_reserve",
            reserve,
            "Including automatic deductions, Continuously retain reserve funds after each actual posting. ",
            timeline=timeline,
        )
        return audit_frame(world, c, r, case, valid, seen, checks)
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        ck(
            "state.well_formed",
            False,
            "Fixed deposit ledger or obligation records are incomplete. ",
            error=str(exc),
        )
        return dict(
            success=False,
            failed=[x["name"] for x in checks if not x["passed"]],
            checks=checks,
            allowed_terminals=["COMPLETED"],
        )
