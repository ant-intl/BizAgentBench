from copy import deepcopy


class PrepaymentOperations:
    def prepayment_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "reset_prepayments", "SERVICE_CONTRACT_MISMATCH")
        self.check(action == "repay_principal", "UNKNOWN_ACTION")
        self.args(data, "loan_id", "amount_minor")
        r = self.local(c, data["loan_id"], "prepayment_loan")
        n = data["amount_minor"]
        self.positive(n)
        self.check(
            r["repayment_from_minute"] <= self.w["minute"] <= r["repayment_through_minute"],
            "REPAYMENT_WINDOW_CLOSED",
        )
        self.check(
            n % r["increment_minor"] == 0
            and n >= r["minimum_prepayment_minor"]
            and n <= r["principal_minor"],
            "INVALID_PRINCIPAL_REPAYMENT",
        )
        fee = r["processing_fee_minor"]
        self.check(c["cash_minor"] >= n + fee, "INSUFFICIENT_REPAYMENT_CASH")
        c["cash_minor"] -= n + fee
        c["fees_minor"] += fee
        r["principal_minor"] -= n
        r["principal_repaid_minor"] += n
        r["processing_paid_minor"] += fee
        return self.book(
            c,
            action,
            contract="reset_prepayments",
            arguments=deepcopy(data),
            receipt=dict(
                loan_ref=r["reference"],
                principal_minor=n,
                fee_minor=fee,
                remaining_principal_minor=r["principal_minor"],
            ),
            cash_delta_minor=-n - fee,
            cash_after_minor=c["cash_minor"],
            fee_minor=fee,
        )

    def prepayment_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        schedule = self.byref(c, p["schedule_ref"])
        before = c["cash_minor"]
        c["cash_minor"] += schedule["receipt_minor"]
        points = [c["cash_minor"]]
        interest = []
        for r in sorted(self.records(c, "prepayment_loan"), key=lambda x: x["reference"]):
            rate = r["period_rates_bp"][str(schedule["interval"])]
            self.check(type(rate) is int, "RATE_NOT_EFFECTIVE")
            charge = r["principal_minor"] * rate // 10000
            paid = c["cash_minor"] >= charge
            if paid:
                c["cash_minor"] -= charge
                r["interest_paid_minor"] += charge
            interest.append(
                dict(
                    loan_ref=r["reference"],
                    principal_minor=r["principal_minor"],
                    rate_bp=rate,
                    interest_minor=charge,
                    paid=paid,
                )
            )
            points.append(c["cash_minor"])
        paid = c["cash_minor"] >= schedule["operating_debit_minor"]
        if paid:
            c["cash_minor"] -= schedule["operating_debit_minor"]
        points.append(c["cash_minor"])
        schedule.update(status="PROCESSED", operating_paid=paid)
        notice = None
        if p["reset_ref"]:
            r = self.byref(c, p["reset_ref"])
            rate = r["_actual_rate_bp"]
            self.check(rate in r["possible_rates_bp"], "INVALID_RATE_OUTCOME")
            r.update(status="FINAL", actual_rate_bp=rate)
            self.byref(c, r["loan_ref"])["period_rates_bp"]["1"] = rate
            notice = dict(loan_ref=r["loan_ref"], rate_bp=rate)
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(
                schedule_ref=schedule["reference"],
                incoming_minor=schedule["receipt_minor"],
                interest=interest,
                operating_debit_minor=schedule["operating_debit_minor"],
                operating_paid=paid,
                cash_path_minor=points,
                rate_notice=notice,
            ),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
