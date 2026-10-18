from copy import deepcopy


class DistributionOperations:
    def _distribution_cash(self, c):
        c["cash_minor"] = sum(r["cash_minor"] for r in self.records(c, "company_cash_account"))

    def distribution_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "effective_distributions", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        receipt = {}
        terms = self.byref(c, "TERMS")
        if action == "pay_distribution":
            self.args(data, "obligation_id", "cash_account_id", "gross_minor", "withholding_minor")
            obligation = self.local(c, data["obligation_id"], "net_distribution_obligation")
            account = self.local(c, data["cash_account_id"], "company_cash_account")
            gross = data["gross_minor"]
            tax = data["withholding_minor"]
            self.positive(gross)
            self.check(type(tax) is int and 0 <= tax < gross, "INVALID_WITHHOLDING")
            net = gross - tax
            self.check(
                net <= obligation["net_target_minor"] - obligation["net_paid_minor"]
                and gross <= account["cash_minor"],
                "DISTRIBUTION_NOT_AVAILABLE",
            )
            account["cash_minor"] -= gross
            tax_account = self.byref(c, terms["tax_account_ref"])
            tax_account["cash_minor"] += tax
            tax_obligation = self.byref(c, "WITHHOLDING")
            tax_obligation["amount_minor"] += tax
            obligation["net_paid_minor"] += net
            obligation["tax_withheld_minor"] += tax
            payment = dict(
                gross_minor=gross,
                withholding_minor=tax,
                net_minor=net,
                cash_account_ref=account["reference"],
                minute=self.w["minute"],
            )
            obligation["payments"].append(payment)
            receipt = dict(
                obligation_ref=obligation["reference"],
                payment=deepcopy(payment),
                remaining_net_minor=obligation["net_target_minor"] - obligation["net_paid_minor"],
            )
        elif action == "remit_withholding":
            self.args(data, "tax_obligation_id", "cash_account_id", "amount_minor")
            obligation = self.local(c, data["tax_obligation_id"], "withholding_tax_obligation")
            account = self.local(c, data["cash_account_id"], "company_cash_account")
            amount = data["amount_minor"]
            self.positive(amount)
            self.check(
                amount <= obligation["amount_minor"] - obligation["paid_minor"]
                and amount <= account["cash_minor"],
                "WITHHOLDING_PAYMENT_NOT_AVAILABLE",
            )
            account["cash_minor"] -= amount
            obligation["paid_minor"] += amount
            payment = dict(
                amount_minor=amount, cash_account_ref=account["reference"], minute=self.w["minute"]
            )
            obligation["payments"].append(payment)
            receipt = dict(
                obligation_ref=obligation["reference"],
                payment=deepcopy(payment),
                remaining_minor=obligation["amount_minor"] - obligation["paid_minor"],
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        self._distribution_cash(c)
        return self.book(
            c,
            action,
            contract="effective_distributions",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=0,
            cash_after_minor=c["cash_minor"],
        )

    def distribution_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        payload = self.w["_event_payloads"][event["id"]]
        self.check(payload["event_kind"] == "tax_evidence_decision", "UNKNOWN_EVENT")
        evidence = self.byref(c, payload["evidence_ref"])
        self.check(evidence["status"] == "REVIEW", "EVIDENCE_ALREADY_DECIDED")
        evidence.update(status=evidence["_actual_status"], rate_bp=evidence["_actual_rate_bp"])
        receipt = dict(
            evidence_ref=evidence["reference"],
            status=evidence["status"],
            rate_bp=evidence["rate_bp"],
        )
        return self.book(
            c,
            "bank_event",
            event_kind=payload["event_kind"],
            event_id=event["id"],
            receipt=receipt,
            cash_delta_minor=0,
            fee_minor=0,
            cash_after_minor=c["cash_minor"],
        )
