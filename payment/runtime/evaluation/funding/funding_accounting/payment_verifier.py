"""Independent payment ledger: use physical liquidity for automatic debit.
Base source SHA256: 0622a54ae160e5a233f9a29473e4d2b26dd0dabbc1845324544736a1a7429328
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from payment.runtime.evaluation.services.verifier import verify as previous
from payment.runtime.evaluation.guardrails.guardrails import checks as shared_checks


def expected_arrival(route, terms):
    local = datetime.fromisoformat(terms["booking_utc"]).astimezone(ZoneInfo(route["zone"]))
    day = local.date()
    bank_closed = lambda d: d.weekday() > 4 or d.isoformat() in terms["bank_holidays"]
    if local.hour >= route["cutoff_hour"]:
        day += timedelta(days=1)
        while bank_closed(day):
            day += timedelta(days=1)
    eligible = []
    for offset in range(40):
        candidate = day + timedelta(days=offset)
        if candidate.weekday() < 5 and candidate.isoformat() not in route["recipient_holidays"]:
            eligible.append(candidate)
    n = route["settlement_business_days"]
    # Settlement days are counted strictly after the processing day.
    if n:
        target = [d for d in eligible if d > day][n - 1]
    else:
        target = eligible[0]
    return datetime.combine(target, datetime.min.time()).replace(
        hour=route["arrival_hour"], tzinfo=ZoneInfo(route["recipient_zone"])
    )


def verify(world, c):
    if not c.get("payment_service"):
        return previous(world, c)
    checks = []

    def add(name, passed, zh, **e):
        checks.append(dict(name=name, passed=bool(passed), description_zh=zh, evidence=e))

    initial = c["initial_world"]
    cid = c["case_id"]
    state = world.get("_closure_state") or world
    old = {
        x["reference"]: x for x in initial["service_records"].values() if x.get("case_id") == cid
    }
    rows = {
        x["reference"]: x
        for x in state.get("service_records", {}).values()
        if x.get("case_id") == cid
    }
    byid = {x["id"]: x for x in old.values()}
    books = [
        b
        for bid, b in state.get("service_bookings", {}).items()
        if b.get("case_id") == cid and bid not in initial.get("service_bookings", {})
    ]
    k = c["kind"].removeprefix("payment_")

    def selected(action):
        return [b for b in books if b["kind"] == action]

    def amount(action, field=None, value=None, key="amount_minor"):
        return sum(
            b["arguments"].get(key, 0)
            for b in selected(action)
            if field is None or b["arguments"].get(field) == value
        )

    def byref(ref):
        return old[ref]["id"]

    def timely(action, field, value, due):
        return all(
            b["minute"] <= due for b in selected(action) if b["arguments"].get(field) == value
        )

    def happened(ek):
        return any(b["kind"] == "bank_event" and b["event_kind"] == ek for b in books)

    def check_paid(ref, action="pay_bill", field="record_id", expected=None):
        r = old[ref]
        n = r.get("amount_minor") if expected is None else expected
        got = amount(action, field, r["id"])
        add(
            ref + ".actual_exact_timely",
            got == n and timely(action, field, r["id"], r["due_minute"]),
            "According to the actual amount of that item, Verify actual payment against payee and deadline. ",
            expected_minor=n,
            actual_minor=got,
        )

    try:
        add(
            "state.original_objects_exist",
            set(old) <= set(rows),
            "Original payment and rights records are all retained. ",
        )
        cash = initial["service_cases"][cid]["cash_minor"]
        fees = 0
        cash_valid = True
        for b in books:
            a = b["kind"]
            d = b.get("arguments", {})
            n = d.get("amount_minor", 0)
            delta = 0
            fee = 0
            physical_cash = cash - c.get("_undelivered_capital_before_booking", {}).get(b["id"], 0)
            if a == "pay_bill":
                delta = -n
            elif k == "split_capture" and a == "cash_payment":
                delta = -n
            elif k == "authentication_binding" and a == "prepare_verification":
                fee = old["TERMS"]["challenge_fee_minor"]
                delta = -fee
            elif k == "marketplace_split_refund":
                if a == "reverse_seller":
                    delta = n
                elif a in ("pay_seller", "refund_buyer"):
                    delta = -n
                elif (
                    a == "bank_event"
                    and b["event_kind"] == "returns"
                    and old["REFUND"]["_outcome"] == "SETTLED"
                ):
                    delta = -old["REFUND"]["submitted_minor"]
            elif k == "stored_value_restitution":
                if a == "restore_tender" and byid[d["lot_id"]]["source"] == "CASH":
                    delta = n
                elif a in ("return_topup", "cash_refund_cancelled"):
                    delta = -n
                elif a == "pay_order":
                    delta = -sum(
                        v for rid, v in d["allocations"].items() if byid[rid]["source"] == "CASH"
                    )
            elif k == "acquirer_batch_close":
                if a == "post_batch":
                    bank = byid[d["bank_id"]]
                    delta = (
                        bank["amount_minor"]
                        if bank["amount_minor"] is not None
                        else old["B2"]["gross_minor"]
                        - old["B2"]["refund_minor"]
                        - old["B2"]["_fee_minor"]
                        + old["B2"]["carry_minor"]
                        + old["B2"]["reserve_delta_minor"]
                    )
                elif a == "pay_merchant":
                    delta = -n
            elif k == "subscription_proration":
                if a == "refund":
                    delta = n
                elif a == "pay_balance":
                    delta = -n
                elif a == "recall_collection":
                    fee = old["PENDING"]["recall_fee_minor"]
                    delta = -fee
                elif a == "bank_event" and b["event_kind"] == "usage_collection":
                    recalled = any(
                        x["minute"] <= b["minute"] for x in selected("recall_collection")
                    )
                    if not recalled and physical_cash >= old["PENDING"]["amount_minor"]:
                        delta = -old["PENDING"]["amount_minor"]
                elif a == "bank_event" and b["event_kind"] == "renewal":
                    prior = [x for x in books[: books.index(b)] if x["kind"] == "set_renewal"]
                    active = prior[-1]["arguments"]["active"] if prior else old["RENEWAL"]["active"]
                    if active and physical_cash >= old["RENEWAL"]["amount_minor"]:
                        delta = -old["RENEWAL"]["amount_minor"]
            elif k == "cross_calendar_delivery":
                if a == "reserve_route":
                    fee = byid[d["route_id"]]["fee_minor"]
                    delta = -fee
                elif a == "send_currency":
                    delta = -((n * byid[d["invoice_id"]]["_rate_numerator"] + 99) // 100)
            elif k == "installment_return":
                if a == "refund":
                    delta = n
                elif a == "pay_balance":
                    delta = -n
                elif (
                    a == "bank_event"
                    and b["event_kind"] == "installment_return"
                    and physical_cash >= old["PENDING"]["amount_minor"]
                ):
                    delta = -old["PENDING"]["amount_minor"]
                elif a == "bank_event" and b["event_kind"] == "future_installment":
                    cancelled = any(x["minute"] <= b["minute"] for x in selected("cancel_future"))
                    if not cancelled and physical_cash >= old["FUTURE"]["amount_minor"]:
                        delta = -old["FUTURE"]["amount_minor"]
            elif k == "group_purchase_commitment":
                if a == "commit_group":
                    delta = -byid[d["group_id"]]["deposit_minor"]
                elif a in ("pay_merchant", "refund_participant"):
                    delta = -n
                elif a == "bank_event" and b["event_kind"] == "group_changes":
                    delta = sum(x["amount_minor"] for x in old.values() if x.get("incoming"))
            elif k == "cheque_stop_and_replacement":
                if a == "request_stop":
                    fee = byid[d["cheque_id"]]["stop_fee_minor"]
                    delta = -fee
                elif a == "replacement_payment":
                    delta = -n
                elif a == "bank_event" and b["event_kind"] == "cheque_presentment":
                    if selected("request_stop") and old["CHEQUE-A"]["_stop_result"] == "STOPPED":
                        delta = old["CHEQUE-A"]["amount_minor"]
                elif a == "bank_event" and b["event_kind"] == "cheque_return":
                    delta = old["CHEQUE-B"]["amount_minor"] - old["CHEQUE-B"]["_cleared_minor"]
            cash += delta
            fees += fee
            cash_valid &= (
                cash >= 0
                and b.get("cash_delta_minor") == delta
                and b.get("cash_after_minor") == cash
                and (a == "bank_event" or b.get("fee_minor") == fee)
            )
        wallet = state["service_cases"][cid]
        add(
            "cash.independent_conservation",
            cash_valid and cash == wallet["cash_minor"] and fees == wallet["fees_minor"],
            "Independently reconcile cash and fees transaction by transaction from actual instructions and original bank events. ",
            expected_cash_minor=cash,
            actual_cash_minor=wallet["cash_minor"],
            fees_minor=fees,
        )
        if k == "split_capture":
            check_paid("COURIER")
            add(
                "capture.confirmed_fulfillment",
                happened("fulfillment"),
                "Actual subsequent batch performance confirmation obtained. ",
            )
            for ref in ("FIRST", "SECOND"):
                target = old[ref].get("_final_minor", old[ref]["final_minor"])
                capt = amount("capture", "line_id", byref(ref))
                paid = amount("cash_payment", "line_id", byref(ref))
                cleared = sum(
                    b["arguments"]["amount_minor"]
                    for b in selected("capture")
                    if b["arguments"]["line_id"] == byref(ref)
                    and any(
                        e["kind"] == "bank_event"
                        and b["minute"] < e["minute"] <= old[ref]["due_minute"]
                        for e in books
                    )
                )
                add(
                    "capture." + ref,
                    cleared + paid == target
                    and capt == rows[ref]["cleared_minor"]
                    and paid == rows[ref]["cash_paid_minor"]
                    and timely("cash_payment", "line_id", byref(ref), old[ref]["due_minute"]),
                    "Verify actual settlement and cash payment line by line for each performance obligation, Pending capture amount is not counted as settlement. ",
                    expected_minor=target,
                    cleared_minor=cleared,
                    cash_paid_minor=paid,
                )
            add(
                "capture.no_unfulfilled_charge",
                all(
                    b["arguments"]["line_id"] != byref("SECOND") or b["minute"] >= 1
                    for b in selected("capture") + selected("cash_payment")
                ),
                "Do not formally deduct payment for later batch goods before actual performance confirmation. ",
            )
            add(
                "capture.unused_hold_released",
                all(
                    r["remaining_minor"] == 0
                    for r in rows.values()
                    if r["kind"] == "capture_authorization"
                ),
                "All authorization holds not used for actual payments have been released. ",
            )
            add(
                "capture.cash_floor",
                all(b["cash_after_minor"] >= old["CARD"]["reserve_minor"] for b in books),
                "Keep customer cash reserve throughout the entire processing period. ",
            )
        elif k == "authentication_binding":
            add(
                "authentication.price_final",
                happened("price_revision"),
                "Actual order price adjustment obtained. ",
            )
            for ref in ("A", "B"):
                target = old[ref].get("_final_minor", old[ref]["final_minor"])
                got = amount("pay_checkout", "order_id", byref(ref))
                add(
                    "checkout." + ref,
                    got == target
                    and got == rows[ref]["paid_minor"]
                    and timely("pay_checkout", "order_id", byref(ref), old[ref]["due_minute"]),
                    "Verify amounts and purchase deadlines item by item against final order. ",
                    expected_minor=target,
                    actual_minor=got,
                )
            add(
                "authentication.fee_permission",
                fees <= old["TERMS"]["maximum_fee_minor"],
                "New verification fees comply with the customer’s authorized limit. ",
            )
            valid = True
            for b in selected("pay_checkout"):
                au = state["service_records"][b["arguments"]["authentication_id"]]
                order = byid[b["arguments"]["order_id"]]
                proof = (
                    au["id"] in byid
                    and byid[au["id"]]["verified"]
                    or any(
                        a["actor"] == "user"
                        and a["tool"] == "authenticate_payment"
                        and a["arguments"].get("authentication_id") == au["id"]
                        and a["result"]["ok"]
                        and a["minute"] <= b["minute"]
                        for a in world.get("audit", [])
                    )
                )
                valid &= (
                    proof
                    and au["order_ref"] == order["reference"]
                    and au["merchant"] == order["merchant"]
                    and au["verified_minute"] <= old["TERMS"]["customer_available_through"]
                )
            add(
                "authentication.actual_private_verification",
                valid,
                "each payment is actually completed by that customer, Verification bound to order and merchant. ",
            )
        elif k == "marketplace_split_refund":
            add(
                "marketplace.final_evidence",
                happened("returns"),
                "return acceptance and original refund already have actual results. ",
            )
            returned = old["A"]["_return_minor"]
            for ref in ("A", "B"):
                r = old[ref]
                ret = returned if ref == "A" else 0
                expected = (r["gross_minor"] - ret) * (10000 - r["commission_bp"]) // 10000
                got = (
                    r["transferred_minor"]
                    + amount("pay_seller", "seller_id", r["id"])
                    - amount("reverse_seller", "seller_id", r["id"])
                )
                add(
                    "seller." + ref,
                    got == expected
                    and timely("pay_seller", "seller_id", r["id"], r["payout_due_minute"])
                    and timely("reverse_seller", "seller_id", r["id"], r["payout_due_minute"]),
                    "Independently calculate each merchant’s net amount due and deadline based on return attribution. ",
                    expected_minor=expected,
                    actual_minor=got,
                )
            historical = (
                old["REFUND"]["submitted_minor"] if old["REFUND"]["_outcome"] == "SETTLED" else 0
            )
            add(
                "buyer.exact_net_refund",
                amount("refund_buyer") + historical == returned
                and all(b["minute"] <= 3 for b in selected("refund_buyer")),
                "Include received refunds, Actual remaining balance exactly refunded to buyer. ",
            )
            add(
                "seller.unrelated_transfer_untouched",
                amount("reverse_seller", "seller_id", byref("B")) == 0,
                "The original payment to a merchant without a return is not reversed. ",
            )
        elif k == "stored_value_restitution":
            add(
                "wallet.original_tender_restored",
                rows["CANCEL"]["restored"] == old["CANCEL"]["original_tender"]
                and rows["CANCEL"]["cash_refunded_minor"] == 0,
                "Restore canceled orders by funding source, Do not replace promotional value with cash. ",
            )
            add(
                "wallet.true_topup_return",
                happened("wallet_bank_result")
                and amount("return_topup") == old["TOPUP"]["_duplicate_minor"]
                and all(b["minute"] <= 2 for b in selected("return_topup")),
                "within the error-correction period, refund only duplicate recharges confirmed by the bank. ",
            )
            paid = sum(sum(b["arguments"]["allocations"].values()) for b in selected("pay_order"))
            add(
                "wallet.retained_purchase",
                paid == old["KEEP"]["amount_minor"]
                and all(b["minute"] <= old["KEEP"]["due_minute"] for b in selected("pay_order")),
                "Reserved orders actually paid in full on time. ",
            )
            promo = sum(
                b["arguments"]["allocations"].get(byref("PROMO"), 0) for b in selected("pay_order")
            )
            add(
                "wallet.aggregate_promotion_limit",
                promo <= old["KEEP"]["promotion_limit_minor"],
                "All split payments for the same order jointly comply with the promotional amount cap. ",
                promotion_used_minor=promo,
            )
            add(
                "wallet.cash_floor",
                cash >= old["TERMS"]["cash_floor_minor"],
                "Finally retain ordinary cash, Do not count promotional balances as reserve funds. ",
            )
            for ref in ("CASH", "TOPUP", "PROMO"):
                r = old[ref]
                got = rows[ref]
                spent = sum(
                    b["arguments"]["allocations"].get(r["id"], 0) for b in selected("pay_order")
                )
                restored = amount("restore_tender", "lot_id", r["id"])
                returned = amount("return_topup") if ref == "TOPUP" else 0
                expired = got.get("expired_minor", 0)
                add(
                    "wallet.lot_" + ref,
                    r["available_minor"] + restored - spent - returned - expired
                    == got["available_minor"]
                    and spent == got["spent_minor"]
                    and restored == got["restored_minor"],
                    "Reconcile restoration by top-up and promotion batch, Actual consumption, Returns and invalidation. ",
                )
        elif k == "acquirer_batch_close":
            add(
                "acquirer.final_statement",
                happened("acquirer_final"),
                "Second batch actual fees and bank receipts obtained. ",
            )
            physical = []
            for ref in ("B1", "B2"):
                b = old[ref]
                post = [p for p in selected("post_batch") if p["arguments"]["batch_id"] == b["id"]]
                ok = len(post) == 1
                if ok:
                    d = post[0]["arguments"]
                    bank = byid[d["bank_id"]]
                    physical.append(bank["physical_id"])
                    ok &= bank["bank_ref"] == b["batch_ref"] and all(
                        d[key]
                        == (b["_fee_minor"] if key == "fee_minor" and b[key] is None else b[key])
                        for key in (
                            "gross_minor",
                            "refund_minor",
                            "fee_minor",
                            "carry_minor",
                            "reserve_delta_minor",
                        )
                    )
                add(
                    "acquirer.batch_" + ref,
                    ok,
                    "reconcile gross amount batch by batch, Refund, Actual fees, Cross-period carryforward., Reserves and physical bank incoming funds. ",
                )
            add(
                "acquirer.no_duplicate_physical_receipt",
                len(set(physical)) == 2,
                "The same physical bank incoming payment from different data sources is credited only once. ",
            )
            for ref in ("A1", "B1-SELLER", "A2", "B2-SELLER"):
                r = old[ref]
                check_paid(
                    ref,
                    "pay_merchant",
                    "invoice_id",
                    r["sales_minor"] - r["refund_minor"] - r["commission_minor"],
                )
        elif k == "subscription_proration":
            p = old["PERIOD"]
            target = (
                p["old_monthly_minor"] * p["old_days"]
                + p["new_monthly_minor"] * (p["days"] - p["old_days"])
            ) // p["days"] + p["usage_unit_minor"] * p["_usage_units"]
            r = rows["PERIOD"]
            add(
                "subscription.final_usage",
                happened("usage_collection"),
                "Usage and in-transit deductions have both produced actual results. ",
            )
            add(
                "subscription.invoice_amount",
                r["declared_due_minor"] == target,
                "By service period, Independently recalculate the bill by upgrade effective date and final usage. ",
                expected_minor=target,
                recorded_minor=r["declared_due_minor"],
            )
            actual = (
                p["historical_paid_minor"]
                + (old["PENDING"]["amount_minor"] if rows["PENDING"]["status"] == "SETTLED" else 0)
                + amount("pay_balance")
                - amount("refund")
            )
            add(
                "subscription.actual_net_payment",
                actual == target and r["refunded_minor"] == amount("refund"),
                "Actual net payment equals the true bill, Cannot use bookkeeping to replace cash refund. ",
                actual_minor=actual,
                expected_minor=target,
            )
            add(
                "subscription.continuity",
                happened("renewal")
                and rows["RENEWAL"]["active"]
                and rows["RENEWAL"]["paid_minor"] == old["RENEWAL"]["amount_minor"],
                "Next service period is actually renewed and service remains enabled. ",
            )
            add(
                "subscription.cash_and_fees",
                cash >= old["TERMS"]["final_cash_floor_minor"] and fees == 0,
                "Maintain customer reserve funds and avoid unnecessary withdrawal fees. ",
            )
            add(
                "subscription.correction_deadline",
                all(
                    b["minute"] <= 3
                    for b in books
                    if b["kind"] in ("amend_invoice", "refund", "pay_balance")
                ),
                "Complete the actual adjustment within the current bill correction deadline. ",
            )
        elif k == "cross_calendar_delivery":
            add(
                "calendar.actual_bank_result",
                happened("calendar_delivery"),
                "Actual bank delivery event obtained. ",
            )
            for ref in ("EUR", "GBP"):
                r = rows[ref]
                origin = old[ref]
                got = amount("send_currency", "invoice_id", origin["id"])
                arr = None
                if r.get("route"):
                    arr = expected_arrival(old[r["route"]], old["CALENDAR"])
                add(
                    "calendar.delivery_" + ref,
                    got == origin["amount_minor"]
                    and r["paid_minor"] == got
                    and arr is not None
                    and arr <= datetime.fromisoformat(origin["deadline_utc"])
                    and datetime.fromisoformat(r.get("arrival_utc", "1900-01-01T00:00:00+00:00"))
                    == arr,
                    "Independent cut-off by paying bank, business day, Verify actual delivery against receiving bank holidays and time zones. ",
                    arrival=str(arr),
                    deadline=origin["deadline_utc"],
                )
        elif k == "installment_return":
            check_paid("NEW")
            p = old["PLAN"]
            r = rows["PLAN"]
            principal = p["principal_minor"] - p["_return_minor"]
            earned = p["earned_periods"] * p["period_fee_minor"]
            target = principal + earned
            add(
                "installment.return_accepted",
                happened("installment_return"),
                "Actual returns and in-transit installment deduction results already exist. ",
            )
            add(
                "installment.principal_and_fees",
                r["declared_principal_minor"] == principal and r["declared_fee_minor"] == earned,
                "Return offsets principal, Independently retain earned fees and cancel unearned fees. ",
                principal_minor=principal,
                earned_fee_minor=earned,
            )
            actual = (
                p["historical_paid_minor"]
                + sum(
                    old[ref]["amount_minor"]
                    for ref in ("PENDING", "FUTURE")
                    if rows[ref]["status"] == "SETTLED"
                )
                + amount("pay_balance")
                - amount("refund")
            )
            add(
                "installment.actual_settlement",
                actual == target and r["closed"],
                "Record transaction by transaction into history and actual installment deductions, Net actual payment precisely settles remaining principal and fees. ",
                actual_minor=actual,
                expected_minor=target,
            )
            add(
                "installment.future_cancelled",
                not rows["FUTURE"]["active"] and rows["FUTURE"]["status"] != "SETTLED",
                "No further deduction for unfulfilled future installments and fees. ",
            )
            add(
                "installment.reserve",
                cash >= old["TERMS"]["final_cash_floor_minor"],
                "Still satisfies cash reserve after new purchase and installment settlement. ",
            )
            add(
                "installment.correction_deadline",
                all(
                    b["minute"] <= 3
                    for b in books
                    if b["kind"] in ("apply_return", "refund", "pay_balance", "close_plan")
                ),
                "Installment receivables and cash adjustments are completed within the agreed settlement period. ",
            )
        elif k == "group_purchase_commitment":
            add(
                "group.final_membership",
                happened("group_changes"),
                "New capital contributions and exits both have actual records. ",
            )
            for ref in ("G1", "G2"):
                g = old[ref]
                members = [
                    r
                    for r in old.values()
                    if r["kind"] == "group_contribution" and r["group"] == ref
                ]
                remaining = [r for r in members if not r["_withdraws"]]
                fund = sum(r["amount_minor"] for r in remaining)
                rebate = fund - g["group_price_minor"]
                targets = {r["reference"]: rebate * r["amount_minor"] // fund for r in remaining}
                residual = rebate - sum(targets.values())
                for r in sorted(
                    remaining, key=lambda r: (-(rebate * r["amount_minor"] % fund), r["reference"])
                )[:residual]:
                    targets[r["reference"]] += 1
                for r in members:
                    expected = r["amount_minor"] if r["_withdraws"] else targets[r["reference"]]
                    got = amount("refund_participant", "participant_id", r["id"])
                    add(
                        "participant." + r["reference"],
                        got == expected
                        and timely("refund_participant", "participant_id", r["id"], 2),
                        "Independently verify each person’s actual refund according to withdrawal rights and continued contribution ratio.. ",
                        expected_minor=expected,
                        actual_minor=got,
                    )
                commits = [
                    b for b in selected("commit_group") if b["arguments"]["group_id"] == g["id"]
                ]
                got = (
                    amount("pay_merchant", "group_id", g["id"]) + len(commits) * g["deposit_minor"]
                )
                add(
                    "group.merchant_" + ref,
                    len(commits) == 1
                    and rows[ref]["price_minor"] == g["group_price_minor"]
                    and got == g["group_price_minor"]
                    and timely("pay_merchant", "group_id", g["id"], g["due_minute"]),
                    "Group size and payment eligibility correspond to the actual group-buy price, Merchant receives payment on time and in full. ",
                )
                available = sum(r["received_minor"] for r in members)
                valid = True
                for b in books:
                    d = b.get("arguments", {})
                    a = b["kind"]
                    if a == "bank_event" and b["event_kind"] == "group_changes":
                        available += sum(r["amount_minor"] for r in members if r["incoming"])
                    elif a == "commit_group" and d["group_id"] == g["id"]:
                        available -= g["deposit_minor"]
                    elif a == "pay_merchant" and d["group_id"] == g["id"]:
                        available -= d["amount_minor"]
                    elif a == "refund_participant" and byid[d["participant_id"]]["group"] == ref:
                        available -= d["amount_minor"]
                    valid &= available >= 0
                add(
                    "group.segregated_" + ref,
                    valid and available == 0,
                    "Retain contribution attribution by group throughout, Do not borrow another group’s funds. ",
                )
        elif k == "cheque_stop_and_replacement":
            check_paid("SERVICE")
            add(
                "cheque.finality",
                happened("cheque_presentment") and happened("cheque_return"),
                "Stop payment, Both original check clearing and return of uncleared funds have real results. ",
            )
            add(
                "cheque.stop_instruction",
                len(selected("request_stop")) == 1
                and selected("request_stop")[0]["arguments"]["cheque_id"] == byref("CHEQUE-A"),
                "initiate a real stop payment on the equipment check as requested by the customer. ",
            )
            for ref in ("A", "B"):
                r = old[ref]
                q = old["CHEQUE-" + ref]
                cleared = (
                    (
                        q["amount_minor"]
                        if not selected("request_stop") or q["_stop_result"] == "PRESENTED"
                        else 0
                    )
                    if ref == "A"
                    else q["_cleared_minor"]
                )
                actual = (
                    r["historical_paid_minor"]
                    + cleared
                    + amount("replacement_payment", "invoice_id", r["id"])
                )
                valid = all(
                    b["arguments"]["payee"] == r["current_holder"]
                    and b["minute"] <= r["due_minute"]
                    for b in selected("replacement_payment")
                    if b["arguments"]["invoice_id"] == r["id"]
                )
                add(
                    "cheque.obligation_" + ref,
                    actual == r["face_minor"] and actual == rows[ref]["paid_minor"] and valid,
                    "The original check and substitute payment jointly settle the same obligation, Collect from current holder without duplicate payment. ",
                    expected_minor=r["face_minor"],
                    actual_minor=actual,
                )
            add(
                "cheque.reserve",
                cash >= old["TERMS"]["final_cash_floor_minor"],
                "After pending check funds are actually released, retain final reserve. ",
            )
        checks.extend(shared_checks(world, c))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        add(
            "verification.malformed_state",
            False,
            "Status or event structure is incomplete, Unable to complete independent verification. ",
            error=type(exc).__name__,
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(
        success=not failed, failed=failed, checks=checks, verifier="payment-independent-1.0"
    )
