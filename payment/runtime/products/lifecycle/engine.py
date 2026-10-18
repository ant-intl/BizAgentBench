"""Payment lifecycle state transitions."""

from copy import deepcopy
from functools import partial
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from payment.runtime.products.banking.engine import BusinessError

from payment.runtime.products.lifecycle.specification import CAPABILITIES, NEW


def delivery_date(route, terms):
    origin = datetime.fromisoformat(terms["booking_utc"]).astimezone(ZoneInfo(route["zone"]))
    day = origin.date()
    if origin.hour >= route["cutoff_hour"]:
        day += timedelta(days=1)
        while day.weekday() >= 5 or day.isoformat() in terms["bank_holidays"]:
            day += timedelta(days=1)
    remaining = route["settlement_business_days"]
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5 and day.isoformat() not in route["recipient_holidays"]:
            remaining -= 1
    while day.weekday() >= 5 or day.isoformat() in route["recipient_holidays"]:
        day += timedelta(days=1)
    return (
        datetime.combine(day, datetime.min.time())
        .replace(hour=route["arrival_hour"], tzinfo=ZoneInfo(route["recipient_zone"]))
        .isoformat()
    )


class PaymentLifecycle:
    def __getattr__(self, name):
        if name in NEW:
            return partial(self.service_instruction, name.removeprefix("propose_"))
        return self.next_handler(PaymentLifecycle).dynamic_attribute(name)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW:
            return self.next_handler(PaymentLifecycle).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        self.check(
            set(arguments) == {"case_id", "action", "data"}
            and type(arguments["case_id"]) is str
            and type(arguments["action"]) is str
            and type(arguments["data"]) is dict,
            "INVALID_CAPABILITY_ARGUMENTS",
        )
        self.row("service_cases", arguments["case_id"])
        return self.public(
            self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def service_instruction(self, kind, case_id, action, data):
        if "propose_" + kind not in NEW:
            return self.next_handler(PaymentLifecycle).service_instruction(
                kind, case_id, action, data
            )
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
        before, fees = c["cash_minor"], c["fees_minor"]
        if action == "pay_bill":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "bill")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            r["paid_minor"] += n
            r["last_paid_minute"] = self.w["minute"]
            out = {"record_id": r["id"], "paid_minor": r["paid_minor"]}
        else:
            out = getattr(self, "do_" + kind.removeprefix("payment_"))(c, action, data)
        return self.book(
            c,
            action,
            contract=kind,
            arguments=deepcopy(data),
            receipt=out or {},
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )

    def paid(self, r, n, key="paid_minor"):
        r[key] += n
        r["last_paid_minute"] = self.w["minute"]

    def do_split_capture(self, c, a, d):
        if a in ("capture", "release"):
            self.args(
                d, "authorization_id", *(["line_id"] if a == "capture" else []), "amount_minor"
            )
            au = self.local(c, d["authorization_id"], "capture_authorization")
            n = d["amount_minor"]
            self.positive(n)
            self.check(
                au["remaining_minor"] >= n and self.w["minute"] <= au["expires_minute"],
                "AUTHORIZATION_AMOUNT_UNAVAILABLE",
            )
            au["remaining_minor"] -= n
            if a == "release":
                au["released_minor"] += n
                return {"released_minor": n}
            r = self.local(c, d["line_id"], "fulfillment_line")
            self.check(r["reference"] in au["order_refs"], "AUTHORIZATION_ORDER_MISMATCH")
            au["captured_minor"] += n
            r["captured_minor"] += n
            r.setdefault("captures", []).append(
                dict(amount_minor=n, minute=self.w["minute"], fulfilled_at_capture=r["fulfilled"])
            )
            return {"captured_minor": n, "clearing_status": "PENDING"}
        if a == "cash_payment":
            self.args(d, "line_id", "amount_minor")
            r = self.local(c, d["line_id"], "fulfillment_line")
            n = d["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            self.paid(r, n, "cash_paid_minor")
            return {"paid_minor": n}
        if a == "new_authorization":
            self.args(d, "amount_minor")
            n = d["amount_minor"]
            self.positive(n)
            card = self.byref(c, "CARD")
            approved = min(n, max(0, card["new_approval_cap_minor"] - card["new_authorized_minor"]))
            self.check(approved > 0, "ISSUER_DECLINED")
            card["new_authorized_minor"] += approved
            return self.public(
                self.new(
                    "service_records",
                    case_id=c["id"],
                    kind="capture_authorization",
                    reference="NEW-" + str(card["new_authorized_minor"]),
                    name="Additional issuer authorization",
                    approved_minor=approved,
                    remaining_minor=approved,
                    captured_minor=0,
                    released_minor=0,
                    order_refs=["FIRST", "SECOND"],
                    expires_minute=3,
                )
            )
        self.check(False, "UNKNOWN_ACTION")

    def do_authentication_binding(self, c, a, d):
        if a == "prepare_verification":
            self.args(d, "order_id", "maximum_minor")
            r = self.local(c, d["order_id"], "checkout_order")
            n = d["maximum_minor"]
            self.positive(n)
            self.charge(c, self.byref(c, "TERMS")["challenge_fee_minor"])
            return self.public(
                self.new(
                    "service_records",
                    case_id=c["id"],
                    kind="authentication",
                    reference="CHALLENGE-" + str(len(self.records(c, "authentication"))),
                    name=r["name"] + " private verification",
                    order_ref=r["reference"],
                    merchant=r["merchant"],
                    maximum_minor=n,
                    verified=False,
                    cancelled=False,
                    captured_minor=0,
                    verified_minute=None,
                )
            )
        if a == "cancel_verification":
            self.args(d, "authentication_id")
            r = self.local(c, d["authentication_id"], "authentication")
            r["cancelled"] = True
            return self.public(r)
        self.check(a == "pay_checkout", "UNKNOWN_ACTION")
        self.args(d, "order_id", "authentication_id", "amount_minor")
        r = self.local(c, d["order_id"], "checkout_order")
        au = self.local(c, d["authentication_id"], "authentication")
        n = d["amount_minor"]
        self.positive(n)
        self.check(
            au["verified"]
            and not au["cancelled"]
            and au["order_ref"] == r["reference"]
            and au["merchant"] == r["merchant"]
            and au["captured_minor"] + n <= au["maximum_minor"],
            "PAYMENT_AUTHENTICATION_NOT_APPLICABLE",
        )
        self.check(
            sum(x["paid_minor"] for x in self.records(c, "checkout_order")) + n
            <= self.byref(c, "TERMS")["card_limit_minor"],
            "CARD_LIMIT_EXCEEDED",
        )
        au["captured_minor"] += n
        self.paid(r, n)
        return {"paid_minor": n, "authentication_id": au["id"]}

    def authenticate_payment(self, authentication_id):
        r = self.row("service_records", authentication_id)
        c = self.row("service_cases", r["case_id"])
        self.check(
            c["kind"] == "payment_authentication_binding"
            and r["kind"] == "authentication"
            and not r["cancelled"],
            "CHALLENGE_UNAVAILABLE",
        )
        self.check(
            self.w["minute"] <= self.byref(c, "TERMS")["customer_available_through"],
            "AUTHENTICATOR_OFFLINE",
        )
        r.update(verified=True, verified_minute=self.w["minute"])
        return self.public(r)

    def do_marketplace_split_refund(self, c, a, d):
        if a in ("reverse_seller", "pay_seller"):
            self.args(d, "seller_id", "amount_minor")
            r = self.local(c, d["seller_id"], "seller_allocation")
            n = d["amount_minor"]
            self.positive(n)
            if a == "reverse_seller":
                self.check(
                    r["reversed_minor"] + n <= r["transferred_minor"] + r["additional_paid_minor"],
                    "REVERSAL_EXCEEDS_ORIGINAL_TRANSFER",
                )
                self.cash(c, n)
                r["reversed_minor"] += n
            else:
                self.cash(c, -n)
                self.paid(r, n, "additional_paid_minor")
            return {"seller_id": r["id"], "amount_minor": n}
        self.check(a == "refund_buyer", "UNKNOWN_ACTION")
        self.args(d, "amount_minor")
        n = d["amount_minor"]
        self.positive(n)
        self.cash(c, -n)
        self.paid(self.byref(c, "REFUND"), n, "manual_paid_minor")
        return {"paid_minor": n}

    def do_stored_value_restitution(self, c, a, d):
        if a == "restore_tender":
            self.args(d, "lot_id", "amount_minor")
            r = self.local(c, d["lot_id"], "wallet_lot")
            o = self.byref(c, "CANCEL")
            n = d["amount_minor"]
            self.positive(n)
            self.check(
                sum(o["restored"].values()) + o["cash_refunded_minor"] + n
                <= sum(o["original_tender"].values()),
                "ORDER_REFUND_EXCEEDS_PAYMENT",
            )
            o["restored"][r["reference"]] = o["restored"].get(r["reference"], 0) + n
            r["available_minor"] += n
            r["restored_minor"] += n
            if r["source"] == "CASH":
                self.cash(c, n)
            return {"lot_id": r["id"], "restored_minor": n}
        if a == "cash_refund_cancelled":
            self.args(d, "amount_minor")
            n = d["amount_minor"]
            self.positive(n)
            o = self.byref(c, "CANCEL")
            self.check(
                sum(o["restored"].values()) + o["cash_refunded_minor"] + n
                <= sum(o["original_tender"].values()),
                "ORDER_REFUND_EXCEEDS_PAYMENT",
            )
            self.cash(c, -n)
            o["cash_refunded_minor"] += n
            return {"bank_refund_minor": n}
        if a == "return_topup":
            self.args(d, "amount_minor")
            n = d["amount_minor"]
            self.positive(n)
            r = self.byref(c, "TOPUP")
            self.check(
                r["duplicate_minor"] is not None and n <= r["available_minor"],
                "TOPUP_NOT_AVAILABLE",
            )
            self.cash(c, -n)
            r["available_minor"] -= n
            r["returned_minor"] += n
            return {"returned_minor": n}
        self.check(a == "pay_order", "UNKNOWN_ACTION")
        self.args(d, "allocations")
        self.check(type(d["allocations"]) is dict and bool(d["allocations"]), "ALLOCATION_REQUIRED")
        o = self.byref(c, "KEEP")
        total = 0
        for rid, n in d["allocations"].items():
            r = self.local(c, rid, "wallet_lot")
            self.positive(n)
            self.check(n <= r["available_minor"], "WALLET_LOT_INSUFFICIENT")
            if r["source"] == "PROMOTION":
                self.check(
                    self.w["minute"] < r["expires_minute"] and n <= o["promotion_limit_minor"],
                    "PROMOTIONAL_TENDER_UNAVAILABLE",
                )
            else:
                self.cash(c, -n)
            r["available_minor"] -= n
            r["spent_minor"] += n
            total += n
        self.paid(o, total)
        o["payments"].append(dict(allocations=deepcopy(d["allocations"]), minute=self.w["minute"]))
        return {"paid_minor": total}

    def do_acquirer_batch_close(self, c, a, d):
        if a == "post_batch":
            self.args(
                d,
                "batch_id",
                "bank_id",
                "gross_minor",
                "refund_minor",
                "fee_minor",
                "carry_minor",
                "reserve_delta_minor",
            )
            b = self.local(c, d["batch_id"], "acquirer_batch")
            r = self.local(c, d["bank_id"], "acquirer_receipt")
            self.check(
                not b["posted"]
                and r["status"] == "SETTLED"
                and not any(
                    x["used"] and x["physical_id"] == r["physical_id"]
                    for x in self.records(c, "acquirer_receipt")
                ),
                "BATCH_OR_PHYSICAL_RECEIPT_UNAVAILABLE",
            )
            for k in ("gross_minor", "refund_minor", "fee_minor"):
                self.nonnegative(d[k])
            for k in ("carry_minor", "reserve_delta_minor"):
                self.check(type(d[k]) is int, "INTEGER_REQUIRED")
            n = (
                d["gross_minor"]
                - d["refund_minor"]
                - d["fee_minor"]
                + d["carry_minor"]
                + d["reserve_delta_minor"]
            )
            self.check(n == r["amount_minor"], "DECLARED_COMPONENTS_DO_NOT_MATCH_BANK_CASH")
            b.update(posted=True, declared=deepcopy(d), bank_physical_id=r["physical_id"])
            r["used"] = True
            self.cash(c, n)
            return {"posted_minor": n, "physical_id": r["physical_id"]}
        self.check(a == "pay_merchant", "UNKNOWN_ACTION")
        self.args(d, "invoice_id", "amount_minor")
        r = self.local(c, d["invoice_id"], "merchant_invoice")
        n = d["amount_minor"]
        self.positive(n)
        self.cash(c, -n)
        self.paid(r, n)
        return {"paid_minor": n}

    def do_subscription_proration(self, c, a, d):
        p = self.byref(c, "PERIOD")
        if a == "amend_invoice":
            self.args(d, "amount_minor")
            self.nonnegative(d["amount_minor"])
            p["declared_due_minor"] = d["amount_minor"]
            return {"recorded_due_minor": d["amount_minor"]}
        if a == "recall_collection":
            self.args(d)
            r = self.byref(c, "PENDING")
            self.check(r["status"] == "PROCESSING", "COLLECTION_NOT_RECALLABLE")
            self.charge(c, r["recall_fee_minor"])
            r["status"] = "RECALLED"
            return {"status": "RECALLED"}
        if a == "set_renewal":
            self.args(d, "active")
            self.check(type(d["active"]) is bool, "BOOLEAN_REQUIRED")
            self.byref(c, "RENEWAL")["active"] = d["active"]
            return {"active": d["active"]}
        self.check(a in ("refund", "pay_balance"), "UNKNOWN_ACTION")
        self.args(d, "amount_minor")
        n = d["amount_minor"]
        self.positive(n)
        if a == "refund":
            self.check(
                n + p["refunded_minor"] <= p["collected_minor"] + p["additional_paid_minor"],
                "REFUND_EXCEEDS_CAPTURE",
            )
            self.cash(c, n)
            p["refunded_minor"] += n
        else:
            self.cash(c, -n)
            p["additional_paid_minor"] += n
        return {"amount_minor": n, "direction": a}

    def do_cross_calendar_delivery(self, c, a, d):
        if a == "reserve_route":
            self.args(d, "invoice_id", "route_id")
            r = self.local(c, d["invoice_id"], "calendar_invoice")
            route = self.local(c, d["route_id"], "calendar_route")
            self.check(
                self.w["minute"] <= self.byref(c, "CALENDAR")["booking_through_minute"]
                and r["route"] is None
                and r["currency"] == route["currency"],
                "ROUTE_RESERVATION_CLOSED_OR_INAPPLICABLE",
            )
            self.charge(c, route["fee_minor"])
            r["route"] = route["reference"]
            return {"reservation": "ACCEPTED", "route_id": route["id"]}
        self.check(a == "send_currency", "UNKNOWN_ACTION")
        self.args(d, "invoice_id", "amount_minor")
        r = self.local(c, d["invoice_id"], "calendar_invoice")
        n = d["amount_minor"]
        self.positive(n)
        self.check(
            r["route"] is not None and r["rate_numerator"] is not None and not r.get("sent_minor"),
            "EXECUTABLE_QUOTE_UNAVAILABLE",
        )
        cost = (n * r["rate_numerator"] + 99) // 100
        self.cash(c, -cost)
        r.update(sent_minor=n, usd_debit_minor=cost, status="PROCESSING")
        return {"status": "PROCESSING", "usd_debit_minor": cost}

    def do_installment_return(self, c, a, d):
        p = self.byref(c, "PLAN")
        if a == "apply_return":
            self.args(d, "remaining_principal_minor", "earned_fee_minor")
            self.nonnegative(d["remaining_principal_minor"])
            self.nonnegative(d["earned_fee_minor"])
            p.update(
                declared_principal_minor=d["remaining_principal_minor"],
                declared_fee_minor=d["earned_fee_minor"],
            )
            return {
                "remaining_principal_minor": d["remaining_principal_minor"],
                "earned_fee_minor": d["earned_fee_minor"],
            }
        if a == "cancel_future":
            self.args(d)
            self.byref(c, "FUTURE")["active"] = False
            return {"status": "CANCELLED"}
        if a == "close_plan":
            self.args(d)
            p["closed"] = True
            return {"status": "CLOSED"}
        self.check(a in ("refund", "pay_balance"), "UNKNOWN_ACTION")
        self.args(d, "amount_minor")
        n = d["amount_minor"]
        self.positive(n)
        if a == "refund":
            self.check(
                n + p["refunded_minor"] <= p["collected_minor"] + p["paid_minor"],
                "REFUND_EXCEEDS_COLLECTION",
            )
            self.cash(c, n)
            p["refunded_minor"] += n
        else:
            self.cash(c, -n)
            p["paid_minor"] += n
        return {"amount_minor": n, "direction": a}

    def do_group_purchase_commitment(self, c, a, d):
        if a == "commit_group":
            self.args(d, "group_id", "offer")
            g = self.local(c, d["group_id"], "purchase_group")
            self.check(
                not g["committed"] and d["offer"] in ("GROUP", "RETAIL"),
                "GROUP_COMMITMENT_UNAVAILABLE",
            )
            rows = [
                x
                for x in self.records(c, "group_contribution")
                if x["group"] == g["reference"] and x["received_minor"] and not x["withdrawn"]
            ]
            eligible = (
                len(rows) >= g["minimum_people"]
                and sum(x["received_minor"] for x in rows) >= g["minimum_contribution_minor"]
            )
            self.check(d["offer"] != "GROUP" or eligible, "GROUP_OFFER_QUALIFICATION_NOT_MET")
            self.cash(c, -g["deposit_minor"])
            g.update(
                committed=True,
                price_minor=(
                    g["group_price_minor"] if d["offer"] == "GROUP" else g["retail_price_minor"]
                ),
                merchant_paid_minor=g["deposit_minor"],
                committed_minute=self.w["minute"],
                participants_at_commit=[r["reference"] for r in rows],
            )
            return {"committed_price_minor": g["price_minor"]}
        if a == "pay_merchant":
            self.args(d, "group_id", "amount_minor")
            g = self.local(c, d["group_id"], "purchase_group")
            n = d["amount_minor"]
            self.positive(n)
            self.check(g["committed"], "GROUP_NOT_COMMITTED")
            self.cash(c, -n)
            self.paid(g, n, "merchant_paid_minor")
            return {"paid_minor": n}
        self.check(a == "refund_participant", "UNKNOWN_ACTION")
        self.args(d, "participant_id", "amount_minor")
        r = self.local(c, d["participant_id"], "group_contribution")
        n = d["amount_minor"]
        self.positive(n)
        self.check(r["refund_minor"] + n <= r["received_minor"], "REFUND_EXCEEDS_CONTRIBUTION")
        self.cash(c, -n)
        self.paid(r, n, "refund_minor")
        return {"participant_id": r["id"], "paid_minor": n}

    def do_cheque_stop_and_replacement(self, c, a, d):
        if a == "request_stop":
            self.args(d, "cheque_id")
            q = self.local(c, d["cheque_id"], "electronic_cheque")
            self.check(
                q["status"] == "ISSUED" and not q["stop_requested"], "STOP_REQUEST_UNAVAILABLE"
            )
            self.charge(c, q["stop_fee_minor"])
            q["stop_requested"] = True
            return {"status": "STOP_REQUESTED"}
        self.check(a == "replacement_payment", "UNKNOWN_ACTION")
        self.args(d, "invoice_id", "payee", "amount_minor")
        r = self.local(c, d["invoice_id"], "cheque_invoice")
        n = d["amount_minor"]
        self.positive(n)
        self.check(d["payee"] in ("SUPPLIER", "FACTOR", "PACKAGER"), "PAYEE_NOT_REGISTERED")
        self.cash(c, -n)
        self.paid(r, n)
        r["payments"].append(
            dict(payee=d["payee"], amount_minor=n, minute=self.w["minute"], source="REPLACEMENT")
        )
        return {"paid_minor": n, "payee": d["payee"]}

    def apply_event(self, event):
        if not event["kind"].startswith("payment_"):
            return self.next_handler(PaymentLifecycle).apply_event(event)
        c = self.w["service_cases"][event["target_id"]]
        k = c["kind"].removeprefix("payment_")
        ek = self.w["_event_payloads"][event["id"]]["event_kind"]
        m = self.w["minute"]
        before = c["cash_minor"]
        out = {}
        if k == "split_capture":
            if ek == "fulfillment":
                r = self.byref(c, "SECOND")
                r.update(final_minor=r["_final_minor"], fulfilled=True)
                out = {"final_shipment_minor": r["final_minor"]}
            if ek in ("fulfillment", "clearing", "deadline"):
                for r in self.records(c, "fulfillment_line"):
                    newly = (
                        sum(x["amount_minor"] for x in r.get("captures", []) if x["minute"] < m)
                        - r["cleared_minor"]
                    )
                    if newly:
                        r["cleared_minor"] += newly
                        r["cleared_minute"] = m
        elif k == "authentication_binding" and ek == "price_revision":
            r = self.byref(c, "A")
            r["final_minor"] = r["_final_minor"]
            out = {"final_price_minor": r["final_minor"]}
        elif k == "marketplace_split_refund" and ek == "returns":
            r = self.byref(c, "A")
            r["accepted_return_minor"] = r["_return_minor"]
            b = self.byref(c, "REFUND")
            b["status"] = b["_outcome"]
            if b["status"] == "SETTLED":
                self.cash(c, -b["submitted_minor"])
                b["paid_minor"] = b["submitted_minor"]
                b["last_paid_minute"] = m
            out = {
                "accepted_return_minor": r["accepted_return_minor"],
                "refund_status": b["status"],
            }
        elif k == "stored_value_restitution" and ek == "wallet_bank_result":
            r = self.byref(c, "TOPUP")
            r["duplicate_minor"] = r["_duplicate_minor"]
            p = self.byref(c, "PROMO")
            p["expired_minor"] = p["available_minor"]
            p["available_minor"] = 0
            out = {
                "duplicate_minor": r["duplicate_minor"],
                "expired_promotion_minor": p["expired_minor"],
            }
        elif k == "acquirer_batch_close" and ek == "acquirer_final":
            r = self.byref(c, "B2")
            r.update(fee_minor=r["_fee_minor"], status="FINAL")
            b = self.byref(c, "BANK2")
            b.update(
                amount_minor=r["gross_minor"]
                - r["refund_minor"]
                - r["fee_minor"]
                + r["carry_minor"]
                + r["reserve_delta_minor"],
                status="SETTLED",
            )
            out = {"actual_bank_credit_minor": b["amount_minor"]}
        elif k == "subscription_proration":
            if ek == "usage_collection":
                p = self.byref(c, "PERIOD")
                p["usage_units"] = p["_usage_units"]
                r = self.byref(c, "PENDING")
                if r["status"] == "PROCESSING":
                    ok = c["cash_minor"] >= r["amount_minor"]
                    r["status"] = "SETTLED" if ok else "UNPAID"
                    if ok:
                        self.cash(c, -r["amount_minor"])
                        p["collected_minor"] += r["amount_minor"]
                out = {"usage_units": p["usage_units"], "collection_status": r["status"]}
            elif ek == "renewal":
                r = self.byref(c, "RENEWAL")
                ok = r["active"] and c["cash_minor"] >= r["amount_minor"]
                if ok:
                    self.cash(c, -r["amount_minor"])
                    self.paid(r, r["amount_minor"])
                out = {"renewal_paid": ok}
        elif k == "cross_calendar_delivery":
            for r in self.records(c, "calendar_invoice"):
                if ek == "fx_rates":
                    r["rate_numerator"] = r["_rate_numerator"]
                elif ek == "calendar_delivery" and r.get("sent_minor"):
                    r.update(
                        paid_minor=r["sent_minor"],
                        arrival_utc=delivery_date(
                            self.byref(c, r["route"]), self.byref(c, "CALENDAR")
                        ),
                        status="SETTLED",
                    )
            out = {"event": ek}
        elif k == "installment_return":
            p = self.byref(c, "PLAN")
            if ek == "installment_return":
                p["return_minor"] = p["_return_minor"]
                r = self.byref(c, "PENDING")
                ok = c["cash_minor"] >= r["amount_minor"]
                r["status"] = "SETTLED" if ok else "UNPAID"
                if ok:
                    self.cash(c, -r["amount_minor"])
                    p["collected_minor"] += r["amount_minor"]
                out = {"accepted_return_minor": p["return_minor"], "collection_status": r["status"]}
            elif ek == "future_installment":
                r = self.byref(c, "FUTURE")
                ok = r["active"] and c["cash_minor"] >= r["amount_minor"]
                if ok:
                    self.cash(c, -r["amount_minor"])
                    p["collected_minor"] += r["amount_minor"]
                    r["status"] = "SETTLED"
                elif not r["active"]:
                    r["status"] = "CANCELLED"
                else:
                    r["status"] = "UNPAID"
                out = {"future_collection_status": r["status"]}
        elif k == "group_purchase_commitment" and ek == "group_changes":
            for r in self.records(c, "group_contribution"):
                if r["incoming"]:
                    self.cash(c, r["amount_minor"])
                    r["received_minor"] = r["amount_minor"]
                    r["incoming"] = False
                if r["_withdraws"]:
                    r["withdrawn"] = True
            out = {
                "withdrawn_participants": [
                    r["reference"] for r in self.records(c, "group_contribution") if r["withdrawn"]
                ]
            }
        elif k == "cheque_stop_and_replacement":
            qa, qb = self.byref(c, "CHEQUE-A"), self.byref(c, "CHEQUE-B")
            if ek == "cheque_presentment":
                qa["stop_result"] = qa["_stop_result"] if qa["stop_requested"] else "PRESENTED"
                qa["status"] = qa["stop_result"]
                r = self.byref(c, "A")
                if qa["status"] == "STOPPED":
                    self.cash(c, qa["held_minor"])
                else:
                    self.paid(r, qa["held_minor"])
                    r["payments"].append(
                        dict(
                            payee=qa["current_holder"],
                            amount_minor=qa["held_minor"],
                            minute=m,
                            source="CHEQUE",
                        )
                    )
                qa["held_minor"] = 0
                qb["cleared_minor"] = qb["_cleared_minor"]
                qb["held_minor"] -= qb["cleared_minor"]
                qb["status"] = "PARTIAL" if qb["held_minor"] else "SETTLED"
                r = self.byref(c, "B")
                self.paid(r, qb["cleared_minor"])
                r["payments"].append(
                    dict(
                        payee=qb["current_holder"],
                        amount_minor=qb["cleared_minor"],
                        minute=m,
                        source="CHEQUE",
                    )
                )
                out = {
                    "stop_result": qa["stop_result"],
                    "packaging_cleared_minor": qb["cleared_minor"],
                }
            elif ek == "cheque_return":
                self.cash(c, qb["held_minor"])
                out = {"returned_minor": qb["held_minor"]}
                qb.update(held_minor=0, status="FINAL")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=ek,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )
