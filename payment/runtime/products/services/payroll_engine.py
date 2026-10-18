from copy import deepcopy


class PayrollOperations:
    def payroll_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "dated_payroll", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        if action == "pay_employee":
            self.args(data, "record_id", "amount_minor", "bank_version")
            r = self.local(c, data["record_id"], "payroll_obligation")
            person = self.byref(c, r["employee"])
            self.check(
                person["status"] == "ACTIVE" and person["bank_version"] == data["bank_version"],
                "EMPLOYEE_BANK_VERSION_MISMATCH",
            )
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["employee_paid_minor"] += data["amount_minor"]
        elif action == "pay_tax":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "payroll_obligation")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["tax_paid_minor"] += data["amount_minor"]
        elif action == "pay_attachment":
            self.args(data, "record_id", "order_id", "amount_minor")
            r = self.local(c, data["record_id"], "payroll_obligation")
            order = self.local(c, data["order_id"], "attachment_order")
            self.check(order["employee"] == r["employee"], "EMPLOYEE_ORDER_MISMATCH")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["allocations"][order["reference"]] = (
                r["allocations"].get(order["reference"], 0) + data["amount_minor"]
            )
            order["paid_minor"] += data["amount_minor"]
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="dated_payroll",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def payroll_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        kind = p["event_kind"]
        cash = c["cash_minor"]
        out = {}
        if kind == "court_decision":
            r = self.byref(c, p["reference"])
            r.update(
                status="PUBLISHED",
                decision=r["_decision"],
                effective_from_period=(
                    r["earliest_affected_period"] if r["_decision"] == "STAY" else None
                ),
            )
            out = dict(
                reference=r["reference"],
                decision=r["decision"],
                effective_from_period=r["effective_from_period"],
            )
        elif kind == "payroll_funding":
            self.cash(c, p["amount_minor"])
            out = dict(amount_minor=p["amount_minor"])
        elif kind != "payroll_window":
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )
