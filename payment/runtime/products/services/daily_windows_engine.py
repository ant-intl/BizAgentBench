from copy import deepcopy


class DailyWindowsOperations:
    def daily_windows_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(
            c["kind"] == "windowed_payment_recovery" and action == "pay",
            "SERVICE_CONTRACT_MISMATCH",
        )
        self.args(data, "invoice_id", "beneficiary_id", "amount_minor")
        invoice = self.local(c, data["invoice_id"], "window_invoice")
        beneficiary = self.local(c, data["beneficiary_id"], "window_beneficiary")
        n = data["amount_minor"]
        self.positive(n)
        now = self.w["minute"]
        window = next((r for r in self.records(c, "payment_window") if r["minute"] == now), None)
        self.check(
            window is not None and beneficiary["active"], "WINDOW_OR_BENEFICIARY_UNAVAILABLE"
        )
        self.check(n % window["payment_increment_minor"] == 0, "INVALID_PAYMENT_INCREMENT")
        used = {g: 0 for g in window["group_limits"]}
        for p in self.records(c, "window_physical_payment"):
            if (
                p["status"] == "IN_FLIGHT"
                or p["status"] == "POSTED"
                and p["submission_window"] == now
            ):
                used[self.byref(c, p["beneficiary_ref"])["legal_group"]] += p["amount_minor"]
        for r in self.records(c, "window_invoice"):
            for p in r["new_payments"]:
                if p["minute"] == now:
                    used[self.byref(c, p["beneficiary_ref"])["legal_group"]] += p["amount_minor"]
        group = beneficiary["legal_group"]
        self.check(
            used[group] + n <= window["group_limits"][group]
            and sum(used.values()) + n <= window["global_limit_minor"],
            "PAYMENT_WINDOW_LIMIT",
        )
        self.check(c["cash_minor"] >= n, "INSUFFICIENT_PAYMENT_CASH")
        c["cash_minor"] -= n
        entry = dict(amount_minor=n, beneficiary_ref=beneficiary["reference"], minute=now)
        invoice["new_payments"].append(entry)
        return self.book(
            c,
            action,
            contract="windowed_payment_recovery",
            arguments=deepcopy(data),
            receipt=dict(invoice_ref=invoice["reference"], payment=deepcopy(entry)),
            cash_delta_minor=-n,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )

    def daily_windows_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        before = c["cash_minor"]
        out = None
        if p["transfer"]:
            t = self.byref(c, p["transfer"])
            self.check(t["status"] == "IN_FLIGHT", "TRANSFER_ALREADY_FINAL")
            outcome = t["_actual_outcome"]
            self.check(outcome in t["possible_outcomes"], "INVALID_SETTLEMENT_OUTCOME")
            t["status"] = outcome
            if outcome == "RETURNED":
                c["cash_minor"] += t["amount_minor"]
            out = dict(transfer_ref=t["reference"], status=outcome)
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(transfer=out),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
