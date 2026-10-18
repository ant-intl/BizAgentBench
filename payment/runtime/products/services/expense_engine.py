from copy import deepcopy


class ExpenseOperations:
    def expense_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "reconciled_expenses", "SERVICE_CONTRACT_MISMATCH")
        self.args(data, "employee_id", "amount_minor", "bank_version")
        r = self.local(c, data["employee_id"], "employee")
        n = data["amount_minor"]
        self.positive(n)
        self.check(r["bank_version"] == data["bank_version"], "BANK_VERSION_MISMATCH")
        cash = c["cash_minor"]
        if action == "reimburse":
            self.cash(c, -n)
            r["reimbursed_minor"] += n
        elif action == "recover_advance":
            self.check(r["recovery_mandate"], "RECOVERY_MANDATE_UNAVAILABLE")
            self.cash(c, n)
            r["recovered_minor"] += n
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="reconciled_expenses",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def expense_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        out = {}
        if p["event_kind"] == "merchant_refund":
            r = self.byref(c, p["reference"])
            leg = self.byref(c, r["payment_reference"])
            account = self.byref(c, leg["account"])
            r.update(status="SETTLED", posted_minor=r["_actual_minor"])
            if account["funding_type"] == "COMPANY":
                self.cash(c, r["posted_minor"])
            out = dict(
                refund_reference=r["reference"],
                posted_minor=r["posted_minor"],
                funding_account=account["reference"],
            )
        elif p["event_kind"] != "marker":
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            receipt=out,
        )
