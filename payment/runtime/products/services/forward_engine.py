from copy import deepcopy


class ForwardOperations:
    def forward_wallet(self, c):
        return dict(USD=c["cash_minor"], EUR=c["state"]["eur_minor"], GBP=c["state"]["gbp_minor"])

    def forward_move(self, c, delta):
        start = self.forward_wallet(c)
        self.check(
            all(start[k] + delta.get(k, 0) >= 0 for k in start), "INSUFFICIENT_CURRENCY_BALANCE"
        )
        c["cash_minor"] += delta.get("USD", 0)
        c["state"]["eur_minor"] += delta.get("EUR", 0)
        c["state"]["gbp_minor"] += delta.get("GBP", 0)

    def forward_release(self, r, processed):
        remaining = r["margin_original_minor"] - (
            processed * r["margin_original_minor"] // r["notional_minor"]
        )
        released = r["margin_minor"] - remaining
        self.check(released >= 0, "INVALID_MARGIN_SETTLEMENT")
        r["margin_minor"] = remaining
        return released

    def forward_book(self, c, kind, start, **kw):
        end = self.forward_wallet(c)
        return self.book(
            c,
            kind,
            wallet_delta_minor={k: end[k] - start[k] for k in start},
            wallet_after_minor=end,
            **kw,
        )

    def forward_confirmed(self, c, r):
        n = r["confirmed_delivery_minor"]
        release = self.forward_release(r, n + r["closed_minor"])
        cost = (n * r["rate_numerator"] + r["rate_denominator"] - 1) // r["rate_denominator"]
        self.forward_move(c, dict(USD=release - cost, EUR=n))
        r.update(delivered_minor=n, status="PARTIALLY_SETTLED")
        return dict(actual_eur_minor=n, margin_released_minor=release, posted=True)

    def forward_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "contingent_forwards", "SERVICE_CONTRACT_MISMATCH")
        start = self.forward_wallet(c)
        out = {}
        fee = 0
        if action in ("exercise", "nominate"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "currency_forward")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["reference"] == ("FLEX" if action == "exercise" else "STERLING"),
                "CONTRACT_DELIVERY_TYPE",
            )
            self.check(
                self.w["minute"] <= r["exercise_through"] and n % r["lot_minor"] == 0,
                "OUTSIDE_EXERCISE_WINDOW_OR_LOT",
            )
            used = r["delivered_minor"] if action == "exercise" else r["nominated_minor"]
            self.check(used + n + r["closed_minor"] <= r["notional_minor"], "EXCEEDS_OPEN_NOTIONAL")
            if action == "exercise":
                release = self.forward_release(r, used + n + r["closed_minor"])
                cost = (n * r["rate_numerator"] + r["rate_denominator"] - 1) // r[
                    "rate_denominator"
                ]
                self.forward_move(c, dict(USD=release - cost, EUR=n))
                r["delivered_minor"] += n
                out = dict(
                    delivered_eur_minor=n, usd_cost_minor=cost, margin_released_minor=release
                )
            else:
                self.forward_move(c, dict(EUR=-n))
                r["nominated_minor"] += n
                out = dict(nominated_eur_minor=n)
        elif action == "settle_confirmed":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "currency_forward")
            self.check(
                r["reference"] == "PENDING" and r["status"] == "CONFIRMED_UNPAID",
                "NO_UNPAID_CONFIRMATION",
            )
            out = self.forward_confirmed(c, r)
        elif action == "close_remainder":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "currency_forward")
            self.check(r["status"] not in ("IN_FLIGHT", "CONFIRMED_UNPAID"), "DELIVERY_NOT_SETTLED")
            committed = r.get("nominated_minor", r["delivered_minor"])
            n = r["notional_minor"] - committed - r["closed_minor"]
            self.check(n > 0, "NO_OPEN_REMAINDER")
            r["closed_minor"] += n
            processed = r["closed_minor"] + r["delivered_minor"]
            release = self.forward_release(r, processed)
            if r["reference"] == "PENDING":
                fee = (n * r["close_fee_bp"] + 9999) // 10000
            elif r["reference"] == "STERLING":
                fee = r["close_fee_minor"] - r["close_fee_paid_minor"]
                r["close_fee_paid_minor"] += fee
            self.forward_move(c, dict(USD=release - fee))
            out = dict(closed_notional_minor=n, margin_released_minor=release, fee_minor=fee)
        elif action == "spot":
            self.args(data, "currency", "side", "amount_minor")
            ccy = data["currency"]
            side = data["side"]
            n = data["amount_minor"]
            self.positive(n)
            self.check(ccy in ("EUR", "GBP") and side in ("BUY", "SELL"), "INVALID_SPOT_PAIR")
            q = self.byref(c, "PRICES")
            key = (
                ("eur_early_ask" if self.w["minute"] <= q["eur_early_through"] else "eur_late_ask")
                if ccy == "EUR" and side == "BUY"
                else ccy.lower() + ("_ask" if side == "BUY" else "_bid")
            )
            num = q[key + "_numerator"]
            den = q[key + "_denominator"]
            usd = (n * num + den - 1) // den if side == "BUY" else n * num // den
            self.forward_move(
                c, {"USD": -usd if side == "BUY" else usd, ccy: n if side == "BUY" else -n}
            )
            out = dict(usd_minor=usd, rate_numerator=num, rate_denominator=den)
        elif action == "pay_invoice":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "treasury_invoice")
            n = data["amount_minor"]
            self.positive(n)
            self.forward_move(c, {r["currency"]: -n})
            r["paid_minor"] += n
            out = dict(currency=r["currency"], paid_minor=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        c["fees_minor"] += fee
        return self.forward_book(
            c, action, start, contract="contingent_forwards", arguments=deepcopy(data), receipt=out
        )

    def forward_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        r = self.byref(c, p["reference"])
        kind = p["event_kind"]
        start = self.forward_wallet(c)
        if kind == "euro_confirmation":
            r["confirmed_delivery_minor"] = r["_actual_delivery_minor"]
            r["status"] = "CONFIRMED_UNPAID"
            n = r["confirmed_delivery_minor"]
            release = n * r["margin_original_minor"] // r["notional_minor"]
            cost = (n * r["rate_numerator"] + r["rate_denominator"] - 1) // r["rate_denominator"]
            out = (
                self.forward_confirmed(c, r)
                if c["cash_minor"] + release >= cost
                else dict(actual_eur_minor=n, margin_released_minor=0, posted=False)
            )
        elif kind == "sterling_settlement":
            n = r["nominated_minor"]
            gbp = n * r["rate_numerator"] // r["rate_denominator"]
            release = self.forward_release(r, n + r["closed_minor"])
            self.forward_move(c, dict(USD=release, GBP=gbp))
            r["delivered_minor"] = n
            r["status"] = "SETTLED"
            out = dict(delivered_gbp_minor=gbp, settled_eur_minor=n, margin_released_minor=release)
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.forward_book(
            c, "bank_event", start, event_kind=kind, event_id=event["id"], receipt=out
        )
