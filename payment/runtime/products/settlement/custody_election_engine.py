from copy import deepcopy


class CustodyElectionOperations:
    def custody_election_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "custody_elective_allocation", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        p = self.byref(c, "POSITION")
        m = self.w["minute"]
        out = {}
        if action == "elect":
            self.args(data, "record_id", "units")
            o = self.local(c, data["record_id"], "elective_offer")
            n = data["units"]
            self.positive(n)
            self.check(
                not o["elected"]
                and m <= o["election_deadline"]
                and n % o["tender_increment"] == 0
                and n <= o["maximum_tender_units"]
                and p["available_units"] >= n,
                "ELECTION_UNAVAILABLE",
            )
            self.cash(c, -n * o["subscription_price_minor"])
            p["available_units"] -= n
            p["locked_units"] += n
            o.update(
                elected=True,
                elected_minute=m,
                tendered_units=n,
                escrow_minor=n * o["subscription_price_minor"],
            )
            out = dict(offer_ref=o["reference"], locked_units=n, escrow_minor=o["escrow_minor"])
        elif action == "deliver_old":
            self.args(data, "units")
            n = data["units"]
            self.positive(n)
            o = self.byref(c, "ORDER")
            self.check(n <= p["available_units"], "OLD_SHARES_LOCKED")
            p["available_units"] -= n
            p["delivered_units"] += n
            o["delivered_units"] += n
            o["delivered_minute"] = m
            out = dict(units=n)
        elif action == "pay_invoice":
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            o = self.byref(c, "ORDER")
            o["paid_minor"] += n
            o["paid_minute"] = m
            out = dict(amount_minor=n)
        elif action in ("redeem_rights", "exercise_rights"):
            self.args(data, "units")
            n = data["units"]
            self.positive(n)
            rights = self.byref(c, "RIGHTS")
            self.check(
                rights["open_minute"] <= m <= rights["due_minute"]
                and n <= rights["remaining_units"],
                "RIGHTS_UNAVAILABLE",
            )
            if action == "redeem_rights":
                amount = n * rights["redemption_minor"]
                self.cash(c, amount)
                rights["redeemed_units"] += n
                rights["cash_received_minor"] += amount
            else:
                self.check(
                    not rights["requires_b_application"] or self.byref(c, "OFFER_B")["elected"],
                    "NO_ORIGINAL_JOINT_APPLICATION",
                )
                amount = n * rights["subscription_cost_minor"]
                self.cash(c, -amount)
                rights["exercised_units"] += n
                rights["subscription_spent_minor"] += amount
                p["new_units_by_class"]["B"] += n * rights["new_b_units_per_right"]
            rights["remaining_units"] -= n
            out = dict(
                units=n,
                consideration_minor=amount,
                new_b_units=(
                    n * rights["new_b_units_per_right"] if action == "exercise_rights" else 0
                ),
            )
        elif action == "pay_registrar_fee":
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            fee = self.byref(c, "REGISTRAR_FEE")
            fee["paid_minor"] += n
            fee["paid_minute"] = m
            out = dict(amount_minor=n)
        elif action == "market_trade":
            self.args(data, "record_id", "side", "units")
            r = self.local(c, data["record_id"], "market_route")
            n = data["units"]
            side = data["side"]
            self.positive(n)
            self.check(
                side in ("BUY", "SELL")
                and r["min_units"] <= n <= r["max_units"]
                and n % r["increment"] == 0
                and r["open_minute"] <= m <= r["close_minute"],
                "MARKET_LOT_UNAVAILABLE",
            )
            cls = r["share_class"]
            if side == "BUY":
                amount = n * r["buy_price_minor"]
                self.cash(c, -amount)
                p["new_units_by_class"][cls] += n
                r["bought_units"] += n
                r["spent_minor"] += amount
            else:
                self.check(p["new_units_by_class"][cls] >= n, "NEW_SHARES_UNAVAILABLE")
                amount = n * r["sell_price_minor"]
                self.cash(c, amount)
                p["new_units_by_class"][cls] -= n
                r["sold_units"] += n
                r["received_minor"] += amount
            out = dict(share_class=cls, side=side, units=n, consideration_minor=amount)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract=c["kind"],
            arguments=deepcopy(data),
            receipt=out,
            fee_minor=0,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def custody_election_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        k = self.w["_event_payloads"][event["id"]]["event_kind"]
        cash = c["cash_minor"]
        out = {}
        p = self.byref(c, "POSITION")
        if k.startswith("allotment_"):
            cls = k[-1]
            o = self.byref(c, "OFFER_" + cls)
            ratio = o["_acceptance_bp"]
            n = o["tendered_units"] * ratio // 10000
            refund = o["escrow_minor"] - n * o["subscription_price_minor"]
            self.cash(c, refund)
            p["locked_units"] -= o["tendered_units"]
            p["exchanged_units"] += n
            p["available_units"] += o["tendered_units"] - n
            p["new_units_by_class"][cls] += n
            o.update(
                accepted_units=n,
                actual_acceptance_bp=ratio,
                escrow_minor=0,
                subscription_spent_minor=n * o["subscription_price_minor"],
                status="FINAL",
            )
            if cls == "A":
                rights = self.byref(c, "RIGHTS")
                issued = n // rights["source_divisor"]
                rights.update(issued_units=issued, remaining_units=issued)
            out = dict(
                share_class=cls,
                accepted_units=n,
                acceptance_bp=ratio,
                returned_old_units=o["tendered_units"] - n,
                refund_minor=refund,
                issued_rights=self.byref(c, "RIGHTS")["issued_units"] if cls == "A" else 0,
            )
        elif k not in ("delivery_deadline", "completion"):
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=k,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )
