"""Independent dated coverage and ledger reconstruction."""

from copy import deepcopy
from itertools import product
from payment.runtime.products.services.temporal_support import audit_frame


def fee_bound(rows, cash, reserve, cap, outcome, forced=None):
    # Independent balance bound. For a chosen complete product set, all published
    # charges and extra collateral are unavoidable and never refundable.
    valid = []
    groups = [
        [
            r
            for r in rows.values()
            if r["kind"] == "guarantee_renewal_offer" and r["guarantee_ref"] == g
        ]
        for g in ("A", "B", "C")
    ]
    for options in product(*groups):
        if forced and any(
            o["reference"] != forced.get(o["guarantee_ref"], o["reference"]) for o in options
        ):
            continue
        act = [
            (
                outcome["activation_minute"]
                if o["reference"] == "A_STANDARD"
                else o["activation_minute"]
            )
            for o in options
        ]
        margins = [
            (
                outcome["required_margin_minor"]
                if o["reference"] == "A_STANDARD"
                else o["required_margin_minor"]
            )
            for o in options
        ]
        need_bridge = act[0] > rows["A"]["old_through"] + 1
        # At integer event instants a renewal at old_through+1 is continuous.
        fees = sum(o["fee_minor"] for o in options) + (
            rows["BRIDGE"]["fee_minor"] if need_bridge else 0
        )
        locked = sum(
            max(0, n - rows[o["guarantee_ref"]]["original_margin_minor"])
            for o, n in zip(options, margins)
        )
        remaining = (
            cash
            - rows["SUPPLIER"]["amount_minor"]
            - rows["INSURANCE"]["amount_minor"]
            - fees
            - locked
        )
        if fees <= cap and remaining >= reserve:
            valid.append(
                dict(
                    offers=[o["reference"] for o in options],
                    fee_minor=fees,
                    final_cash_minor=remaining,
                )
            )
    return dict(
        feasible=bool(valid),
        minimum_fees_minor=min((x["fee_minor"] for x in valid), default=None),
        candidates=valid,
        scope="Complete offer sets and necessary bridge cost; nonrefundable charge and collateral lower bound. Reference establishes temporal attainability of the reported minimum.",
    )


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
        valid = True
        seen = set()
        continuous = True
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
            if b.get("contract") == "continuous_guarantees":
                d = b["arguments"]
                if kind == "submit_renewal":
                    o = r[ids[d["offer_id"]]]
                    g = r[o["guarantee_ref"]]
                    valid &= (
                        o["kind"] == "guarantee_renewal_offer"
                        and g["selected_offer"] is None
                        and now <= g["submission_deadline"]
                    )
                    fee = o["fee_minor"]
                    case["cash_minor"] -= fee
                    g.update(
                        selected_offer=o["reference"],
                        submitted_minute=now,
                        new_through=o["coverage_through"],
                    )
                elif kind in ("add_margin", "withdraw_margin"):
                    g = r[ids[d["guarantee_id"]]]
                    n = d["amount_minor"]
                    valid &= g["kind"] == "continuing_guarantee" and type(n) is int and n > 0
                    delta = n if kind == "add_margin" else -n
                    g["margin_minor"] += delta
                    case["cash_minor"] -= delta
                    valid &= g["margin_minor"] >= 0
                elif kind == "buy_bridge":
                    o = r[ids[d["offer_id"]]]
                    g = r[o["guarantee_ref"]]
                    valid &= (
                        o["kind"] == "temporary_guarantee_offer"
                        and g["bridge_through"] is None
                        and now <= o["available_through"]
                    )
                    g["bridge_through"] = o["coverage_through"]
                    fee = o["fee_minor"]
                    case["cash_minor"] -= fee
                elif kind == "pay_supplier":
                    x = r[ids[d["bill_id"]]]
                    n = d["amount_minor"]
                    valid &= x["kind"] == "guarantee_bill" and type(n) is int and n > 0
                    case["cash_minor"] -= n
                    x["paid_minor"] += n
                    x["paid_minute"] = now
                else:
                    valid = False
            elif kind == "bank_event":
                eid = b["event_id"]
                valid &= eid not in seen and i["events"][eid]["due_minute"] == now
                seen.add(eid)
                notice = r["REVIEW"]
                if now == notice["effective_minute"]:
                    actual = deepcopy(notice["_actual"])
                    valid &= actual in notice["possible_outcomes"]
                    notice.update(status="FINAL", actual=actual)
                    r["A_STANDARD"].update(actual)
                activated = []
                for g in [x for x in r.values() if x["kind"] == "continuing_guarantee"]:
                    o = r.get(g["selected_offer"])
                    if (
                        o
                        and not g["active"]
                        and o["activation_minute"] is not None
                        and now >= o["activation_minute"]
                        and g["margin_minor"] >= o["required_margin_minor"]
                    ):
                        g.update(active=True, activation_minute=now)
                        activated.append(g["reference"])
                x = r["INSURANCE"]
                if now == x["due_minute"] and case["cash_minor"] >= x["amount_minor"]:
                    case["cash_minor"] -= x["amount_minor"]
                    x["paid"] = True
                valid &= b["receipt"] == dict(
                    activated=activated, admission=deepcopy(notice["actual"]) if now == 1 else None
                )
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
            covered = []
            for g in [x for x in r.values() if x["kind"] == "continuing_guarantee"]:
                o = r.get(g["selected_offer"])
                old = now <= g["old_through"] and g["margin_minor"] >= g["original_margin_minor"]
                bridge = (
                    g["bridge_through"] is not None
                    and now <= g["bridge_through"]
                    and g["margin_minor"] >= g["original_margin_minor"]
                )
                new = (
                    g["active"]
                    and now <= g["new_through"]
                    and g["margin_minor"] >= o["required_margin_minor"]
                )
                covered.append(bool(old or bridge or new))
            continuous &= all(covered)
            timeline.append(
                dict(booking=b["id"], minute=now, covered=covered, cash_minor=case["cash_minor"])
            )
        bound = fee_bound(
            original,
            i["service_cases"][cid]["cash_minor"],
            c["reserve_minor"],
            c["fee_cap_minor"],
            r["REVIEW"]["_actual"],
        )
        ck(
            "guarantees.continuous_actual_cover",
            continuous
            and r["REVIEW"]["status"] == "FINAL"
            and r["INSURANCE"]["paid"]
            and all(
                r[g]["active"]
                and r[g]["new_through"] >= r[g]["required_through"]
                and r[g]["margin_minor"] >= r[r[g]["selected_offer"]]["required_margin_minor"]
                for g in ("A", "B", "C")
            ),
            "For each operation and actual event, maintain three safeguards, Renewal acceptance does not equal effectiveness. ",
            timeline=timeline,
        )
        ck(
            "payments.actual_supplier_and_debit",
            r["SUPPLIER"]["paid_minor"] == r["SUPPLIER"]["amount_minor"]
            and r["SUPPLIER"]["paid_minute"] is not None
            and r["SUPPLIER"]["paid_minute"] <= r["SUPPLIER"]["due_minute"]
            and r["INSURANCE"]["paid"],
            "equipment fees and scheduled insurance deductions are all genuinely, fully, and timely completed. ",
        )
        ck(
            "cash.continuous_reserve",
            reserve,
            "Fee per occurrence, Maintain wallet reserve after deposit and automatic deductions. ",
        )
        ck(
            "fees.minimum_complete_plan",
            bound["feasible"]
            and case["fees_minor"] == bound["minimum_fees_minor"]
            and case["fees_minor"] <= c["fee_cap_minor"],
            "Independently calculate the minimum non-refundable fee for the complete protection plan. ",
            actual=case["fees_minor"],
            certificate=bound,
        )
        return audit_frame(world, c, r, case, valid, seen, checks)
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        ck("state.well_formed", False, "Business ledger is incomplete. ", error=str(exc))
        return dict(
            success=False,
            failed=[x["name"] for x in checks if not x["passed"]],
            checks=checks,
            allowed_terminals=["COMPLETED"],
        )
