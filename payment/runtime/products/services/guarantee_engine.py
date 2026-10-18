from copy import deepcopy


class GuaranteeOperations:
    def guarantee_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "continuous_guarantees", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        fees = c["fees_minor"]
        now = self.w["minute"]
        if action == "submit_renewal":
            self.args(data, "offer_id")
            o = self.local(c, data["offer_id"], "guarantee_renewal_offer")
            g = self.byref(c, o["guarantee_ref"])
            self.check(
                g["selected_offer"] is None and now <= g["submission_deadline"],
                "RENEWAL_NOT_AVAILABLE",
            )
            self.check(c["cash_minor"] >= o["fee_minor"], "INSUFFICIENT_CASH")
            c["cash_minor"] -= o["fee_minor"]
            c["fees_minor"] += o["fee_minor"]
            g.update(
                selected_offer=o["reference"],
                submitted_minute=now,
                new_through=o["coverage_through"],
            )
        elif action in ("add_margin", "withdraw_margin"):
            self.args(data, "guarantee_id", "amount_minor")
            g = self.local(c, data["guarantee_id"], "continuing_guarantee")
            n = data["amount_minor"]
            self.positive(n)
            if action == "add_margin":
                self.check(c["cash_minor"] >= n, "INSUFFICIENT_CASH")
                c["cash_minor"] -= n
                g["margin_minor"] += n
            else:
                self.check(g["margin_minor"] >= n, "INSUFFICIENT_MARGIN")
                g["margin_minor"] -= n
                c["cash_minor"] += n
        elif action == "buy_bridge":
            self.args(data, "offer_id")
            o = self.local(c, data["offer_id"], "temporary_guarantee_offer")
            g = self.byref(c, o["guarantee_ref"])
            self.check(
                g["bridge_through"] is None and now <= o["available_through"],
                "BRIDGE_NOT_AVAILABLE",
            )
            self.check(c["cash_minor"] >= o["fee_minor"], "INSUFFICIENT_CASH")
            c["cash_minor"] -= o["fee_minor"]
            c["fees_minor"] += o["fee_minor"]
            g["bridge_through"] = o["coverage_through"]
        elif action == "pay_supplier":
            self.args(data, "bill_id", "amount_minor")
            b = self.local(c, data["bill_id"], "guarantee_bill")
            n = data["amount_minor"]
            self.positive(n)
            self.check(c["cash_minor"] >= n, "INSUFFICIENT_CASH")
            c["cash_minor"] -= n
            b["paid_minor"] += n
            b["paid_minute"] = now
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="continuous_guarantees",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )

    def guarantee_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        now = self.w["minute"]
        before = c["cash_minor"]
        activated = []
        n = self.byref(c, "REVIEW")
        if now == n["effective_minute"]:
            actual = deepcopy(n["_actual"])
            self.check(actual in n["possible_outcomes"], "INVALID_ADMISSION")
            n.update(status="FINAL", actual=actual)
            self.byref(c, "A_STANDARD").update(actual)
        for g in self.records(c, "continuing_guarantee"):
            if not g["selected_offer"] or g["active"]:
                continue
            o = self.byref(c, g["selected_offer"])
            if (
                o["activation_minute"] is not None
                and now >= o["activation_minute"]
                and g["margin_minor"] >= o["required_margin_minor"]
            ):
                g.update(active=True, activation_minute=now)
                activated.append(g["reference"])
        d = self.byref(c, "INSURANCE")
        if now == d["due_minute"] and c["cash_minor"] >= d["amount_minor"]:
            c["cash_minor"] -= d["amount_minor"]
            d["paid"] = True
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(
                activated=activated, admission=deepcopy(n["actual"]) if now == 1 else None
            ),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
