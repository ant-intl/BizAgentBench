from copy import deepcopy


class BillingOperations:
    def billing_eligible(self, c, group, age):
        return any(
            r["group"] == group and r["status"] == "ACTIVE" and r["partner_age_days"] >= age
            for r in self.records(c, "billing_account")
        )

    def billing_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "prospective_billing", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        if action == "select_plan":
            self.args(data, "record_id", "plan_id")
            r = self.local(c, data["record_id"], "billing_period")
            p = self.local(c, data["plan_id"], "subscription_plan")
            self.check(
                r["status"] == "OPEN" and self.w["minute"] < r["billing_minute"],
                "BILLING_ELECTION_CLOSED",
            )
            self.check(p["status"] == "AVAILABLE", "PLAN_UNAVAILABLE")
            r["plan"] = p["reference"]
        elif action == "reserve_voucher":
            self.args(data, "record_id", "code")
            r = self.local(c, data["record_id"], "billing_period")
            v = self.byref(c, "VOUCHER")
            self.check(
                r["status"] == "OPEN" and self.w["minute"] < r["billing_minute"],
                "BILLING_ELECTION_CLOSED",
            )
            self.check(v["_owned"] and data["code"] == v["_valid_code"], "CAMPAIGN_CODE_INVALID")
            self.check(v["status"] in ("AVAILABLE", "RESERVED"), "VOUCHER_NOT_AVAILABLE")
            self.check(
                self.billing_eligible(c, v["group"], v["partner_age_days"]),
                "CAMPAIGN_ELIGIBILITY_NOT_MET",
            )
            v.update(status="RESERVED", reserved_period=r["reference"])
        elif action == "close_account":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "billing_account")
            self.check(r["status"] == "ACTIVE", "ACCOUNT_NOT_ACTIVE")
            r.update(status="CLOSED", closed_minute=self.w["minute"])
            v = self.byref(c, "VOUCHER")
            if v["status"] in ("AVAILABLE", "RESERVED") and not self.billing_eligible(
                c, v["group"], v["partner_age_days"]
            ):
                v.update(status="VOID", reserved_period=None)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="prospective_billing",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def billing_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        r = self.byref(c, self.w["_event_payloads"][event["id"]]["reference"])
        p = self.byref(c, r["plan"])
        v = self.byref(c, "VOUCHER")
        u = r["_actual_usage"]
        cash = c["cash_minor"]
        counts = {k: sum(q[k] for q in u.values()) for k in ("domestic", "overseas", "fx")}
        variable = sum(counts[k] * p["rates_minor"][k] for k in counts)
        credit = (
            min(variable, p["usage_credit_minor"])
            if sum(counts.values()) >= p["credit_minimum_transactions"]
            and self.billing_eligible(c, r["group"], p["credit_partner_age_days"])
            else 0
        )
        discount = (
            min(v["cap_minor"], p["fixed_minor"])
            if v["status"] == "RESERVED"
            and v["reserved_period"] == r["reference"]
            and self.billing_eligible(c, r["group"], v["partner_age_days"])
            else 0
        )
        consume = v["status"] == "RESERVED" and v["reserved_period"] == r["reference"]
        n = p["fixed_minor"] + variable - credit - discount
        paid = c["cash_minor"] >= n
        if paid:
            self.charge(c, n)
            if consume:
                v.update(status="CONSUMED", consumed_period=r["reference"])
        r.update(
            status="PAID" if paid else "UNPAID",
            actual_usage=deepcopy(u),
            charge_minor=n,
            coupon_discount_minor=discount,
            usage_credit_applied_minor=credit,
            billed_minute=self.w["minute"],
        )
        return self.book(
            c,
            "bank_event",
            event_kind="subscription_billing",
            event_id=event["id"],
            receipt=dict(
                reference=r["reference"],
                plan=r["plan"],
                actual_usage=deepcopy(u),
                fixed_minor=p["fixed_minor"],
                variable_minor=variable,
                usage_credit_minor=credit,
                coupon_discount_minor=discount,
                charge_minor=n,
                paid=paid,
            ),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )
