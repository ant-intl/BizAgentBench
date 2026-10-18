from copy import deepcopy


class InterestOperations:
    def interest_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "dated_interest", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        receipt = {}
        if action == "record_value_date":
            self.args(data, "item_id", "bank_message_id", "value_day")
            item = self.local(c, data["item_id"], "dated_bank_item")
            message = self.local(c, data["bank_message_id"], "bank_value_confirmation")
            day = data["value_day"]
            self.check(type(day) is int and 1 <= day <= 31, "INVALID_CALENDAR_DAY")
            self.check(
                message["verified_genuine"] and message["status"] == "FINAL",
                "FINAL_BANK_CORRESPONDENCE_REQUIRED",
            )
            number = len(self.records(c, "economic_date_amendment")) + 1
            entry = self.new(
                "service_records",
                case_id=c["id"],
                reference="DATE-" + str(number),
                name="Economic-date amendment " + str(number),
                kind="economic_date_amendment",
                customer_access=True,
                item_ref=item["reference"],
                message_ref=message["reference"],
                value_day=day,
                minute=self.w["minute"],
                status="POSTED",
                source="SERVICE_OPERATION",
            )
            item["current_economic_day"] = day
            item["amendment_refs"].append(entry["reference"])
            receipt = dict(amendment=deepcopy(entry))
        elif action == "post_adjustment":
            self.args(data, "statement_id", "period_id", "amount_minor")
            statement = self.local(c, data["statement_id"], "signed_interest_statement")
            period = self.local(c, data["period_id"], "accounting_period")
            n = data["amount_minor"]
            account = self.byref(c, statement["account_ref"])
            self.check(type(n) is int and n != 0, "NONZERO_SIGNED_AMOUNT_REQUIRED")
            self.check(period["status"] == "OPEN", "ACCOUNTING_PERIOD_CLOSED")
            self.check(
                account["status"] == "OPEN" and account["cash_minor"] + n >= 0,
                "ACCOUNT_CASH_INSUFFICIENT",
            )
            account["cash_minor"] += n
            number = len(self.records(c, "interest_cash_adjustment")) + 1
            entry = self.new(
                "service_records",
                case_id=c["id"],
                reference="ADJUSTMENT-" + str(number),
                name="Current interest cash adjustment " + str(number),
                kind="interest_cash_adjustment",
                customer_access=True,
                account_ref=account["reference"],
                statement_ref=statement["reference"],
                period_ref=period["reference"],
                amount_minor=n,
                status="POSTED",
                minute=self.w["minute"],
                source="SERVICE_OPERATION",
                possible_results=[],
            )
            receipt = dict(adjustment=deepcopy(entry))
        elif action == "pay_accountant":
            self.args(data, "obligation_id", "payer_account_id", "recipient_id", "amount_minor")
            o = self.local(c, data["obligation_id"], "accounting_fee_obligation")
            account = self.local(c, data["payer_account_id"], "interest_account")
            recipient = self.local(c, data["recipient_id"], "interest_recipient")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                account["status"] == "OPEN"
                and recipient["status"] == "VERIFIED"
                and account["currency"] == recipient["currency"] == "USD",
                "PAYMENT_ACCOUNT_OR_RECIPIENT_UNAVAILABLE",
            )
            self.check(account["cash_minor"] >= n, "ACCOUNT_CASH_INSUFFICIENT")
            account["cash_minor"] -= n
            payment = dict(
                payer_ref=account["reference"],
                recipient_ref=recipient["reference"],
                amount_minor=n,
                minute=self.w["minute"],
            )
            o["paid_minor"] += n
            o["payments"].append(payment)
            receipt = dict(obligation_ref=o["reference"], payment=deepcopy(payment))
        else:
            self.check(False, "UNKNOWN_ACTION")
        c["cash_minor"] = sum(r["cash_minor"] for r in self.records(c, "interest_account"))
        return self.book(
            c,
            action,
            contract="dated_interest",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=0,
            cash_after_minor=c["cash_minor"],
        )

    def interest_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        kind = self.w["_event_payloads"][event["id"]]["event_kind"]
        self.check(kind == "refund_result", "UNKNOWN_EVENT")
        results = []
        for r in self.records(c, "interest_cash_adjustment"):
            if r["status"] == "PROCESSING":
                status = r["_actual_result"]
                self.check(status in ("SETTLED", "RETURNED"), "INVALID_BANK_RESULT")
                r.update(status=status, minute=self.w["minute"])
                cash = r["amount_minor"] if status == "SETTLED" else 0
                self.byref(c, r["account_ref"])["cash_minor"] += cash
                results.append(
                    dict(adjustment_ref=r["reference"], status=status, actual_cash_minor=cash)
                )
        c["cash_minor"] = sum(r["cash_minor"] for r in self.records(c, "interest_account"))
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=dict(results=results),
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=0,
            cash_after_minor=c["cash_minor"],
        )
