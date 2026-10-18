from copy import deepcopy


class PresentmentOperations:
    def _presentment_cash(self, c):
        c["cash_minor"] = sum(r["cash_minor"] for r in self.records(c, "company_cash_account"))

    def presentment_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "late_card_reconciliation", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        fees = c["fees_minor"]
        receipt = {}
        terms = self.byref(c, "CARD_TERMS")
        if action == "file_dispute":
            self.args(data, "presentment_id", "amount_minor")
            p = self.local(c, data["presentment_id"], "card_presentment")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                n <= p["amount_minor"] and not p["claim_refs"], "DISPUTE_NOT_OPEN_OR_AMOUNT_INVALID"
            )
            operating = self.byref(c, terms["payment_account_ref"])
            self.check(
                operating["cash_minor"] >= terms["dispute_fee_minor"],
                "DISPUTE_FEE_CASH_INSUFFICIENT",
            )
            operating["cash_minor"] -= terms["dispute_fee_minor"]
            c["fees_minor"] += terms["dispute_fee_minor"]
            number = len(self.records(c, "card_dispute_claim")) + 1
            claim = self.new(
                "service_records",
                case_id=c["id"],
                reference="CLAIM-" + str(number),
                name="Card dispute claim " + str(number),
                kind="card_dispute_claim",
                customer_access=True,
                presentment_ref=p["reference"],
                amount_minor=n,
                status="PROCESSING",
                filed_minute=self.w["minute"],
                decision_minute=None,
                possible_results=["APPROVED", "REJECTED"],
                _actual_result=p["_actual_dispute_result"],
            )
            event = self.schedule(
                "card_reconciliation_event",
                c["id"],
                1,
                event_kind="dispute_decision",
                claim_ref=claim["reference"],
            )
            claim["event_id"] = event["id"]
            p["claim_refs"].append(claim["reference"])
            receipt = dict(claim=self.public(claim))
        elif action == "pay_card":
            self.args(data, "card_account_id", "cash_account_id", "amount_minor")
            card = self.local(c, data["card_account_id"], "corporate_card_account")
            account = self.local(c, data["cash_account_id"], "company_cash_account")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                card["status"] == "OPEN"
                and account["status"] == "OPEN"
                and account["currency"] == card["currency"] == "USD",
                "CARD_OR_CASH_ACCOUNT_UNAVAILABLE",
            )
            self.check(
                n <= card["current_due_minor"] and n <= account["cash_minor"],
                "CARD_PAYMENT_NOT_AVAILABLE",
            )
            account["cash_minor"] -= n
            card["current_due_minor"] -= n
            card["paid_minor"] += n
            payment = dict(
                cash_account_ref=account["reference"], amount_minor=n, minute=self.w["minute"]
            )
            card["payments"].append(payment)
            receipt = dict(
                card_ref=card["reference"],
                payment=deepcopy(payment),
                remaining_due_minor=card["current_due_minor"],
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        self._presentment_cash(c)
        return self.book(
            c,
            action,
            contract="late_card_reconciliation",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=c["fees_minor"] - fees,
            cash_after_minor=c["cash_minor"],
        )

    def presentment_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        payload = self.w["_event_payloads"][event["id"]]
        before = c["cash_minor"]
        kind = payload["event_kind"]
        receipt = {}
        if kind == "merchant_credit":
            credit = self.byref(c, payload["reference"])
            self.check(credit["status"] == "PROCESSING", "CREDIT_ALREADY_RESOLVED")
            credit.update(
                status="POSTED",
                amount_minor=credit["_actual_amount_minor"],
                posted_minute=self.w["minute"],
            )
            card = self.byref(c, credit["settling_card_ref"])
            self.check(
                card["current_due_minor"] >= credit["amount_minor"], "CREDIT_EXCEEDS_CARD_DUE"
            )
            card["current_due_minor"] -= credit["amount_minor"]
            receipt = dict(
                credit_ref=credit["reference"],
                amount_minor=credit["amount_minor"],
                settling_card_ref=card["reference"],
            )
        elif kind == "dispute_decision":
            claim = self.byref(c, payload["claim_ref"])
            self.check(claim["status"] == "PROCESSING", "CLAIM_ALREADY_DECIDED")
            claim.update(status=claim["_actual_result"], decision_minute=self.w["minute"])
            if claim["status"] == "APPROVED":
                presentment = self.byref(c, claim["presentment_ref"])
                card = self.byref(c, presentment["card_ref"])
                self.check(
                    card["current_due_minor"] >= claim["amount_minor"], "DISPUTE_EXCEEDS_CARD_DUE"
                )
                card["current_due_minor"] -= claim["amount_minor"]
            receipt = dict(
                claim_ref=claim["reference"],
                status=claim["status"],
                amount_minor=claim["amount_minor"],
            )
        else:
            self.check(False, "UNKNOWN_EVENT")
        self._presentment_cash(c)
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=0,
            cash_after_minor=c["cash_minor"],
        )
