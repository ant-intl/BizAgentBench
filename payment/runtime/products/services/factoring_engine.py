from copy import deepcopy


class FactoringOperations:
    def factoring_face(self, c, invoice):
        notes = {}
        for n in self.records(c, "buyer_credit_note"):
            if (
                n["invoice_ref"] == invoice["reference"]
                and n["status"] in ("ISSUED", "FINAL", "CANCELLED")
                and n["effective_minute"] <= self.w["minute"]
            ):
                if n["series"] not in notes or n["version"] > notes[n["series"]]["version"]:
                    notes[n["series"]] = n
        credits = sum(n["amount_minor"] for n in notes.values() if n["status"] != "CANCELLED")
        receipts = sum(
            p["amount_minor"]
            for p in self.records(c, "debtor_cash_posting")
            if p["invoice_ref"] == invoice["reference"] and p["status"] == "SETTLED"
        )
        return max(0, invoice["gross_minor"] - credits - receipts)

    def factoring_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "priority_factoring", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        beforefees = c["fees_minor"]
        receipt = {}
        if action == "fund":
            self.args(data, "invoice_id", "offer_id")
            inv = self.local(c, data["invoice_id"], "factoring_invoice")
            offer = self.local(c, data["offer_id"], "invoice_finance_offer")
            self.check(not inv["assigned"], "CLAIM_ALREADY_ASSIGNED")
            self.check(
                not offer["requires_confirmed_notice"] or inv["notice_status"] == "CONFIRMED",
                "NOTICE_NOT_CONFIRMED",
            )
            basis = self.factoring_face(c, inv)
            self.positive(basis)
            advance = basis * offer["advance_bp"] // 10000
            fee = basis * offer["fee_bp"] // 10000
            facilities = [
                f
                for f in self.records(c, "senior_invoice_facility")
                if inv["reference"] in f["collateral_refs"]
            ]
            payoff = sum(f["principal_minor"] for f in facilities)
            self.check(advance >= payoff + fee, "ADVANCE_INSUFFICIENT_FOR_PRIORITY")
            self.cash(c, advance - payoff)
            self.charge(c, fee)
            c["state"]["finance_fees_minor"] += fee
            for f in facilities:
                f.update(principal_minor=0, status="REPAID")
            inv.update(
                assigned=True,
                factor_offer_ref=offer["reference"],
                advance_minor=advance,
                finance_fee_minor=fee,
                funded_minute=self.w["minute"],
            )
            for cover in self.records(c, "export_cover"):
                if inv["reference"] in cover["invoice_refs"]:
                    cover["status"] = "SUSPENDED"
            receipt = dict(
                invoice_ref=inv["reference"],
                offer_ref=offer["reference"],
                eligible_face_minor=basis,
                advance_minor=advance,
                senior_payoff_minor=payoff,
                finance_fee_minor=fee,
                net_wallet_minor=advance - payoff - fee,
            )
        elif action == "pay_local":
            self.args(data, "obligation_id", "beneficiary_id", "amount_minor")
            o = self.local(c, data["obligation_id"], "local_payment_obligation")
            b = self.local(c, data["beneficiary_id"], "factoring_beneficiary")
            n = data["amount_minor"]
            self.positive(n)
            self.check(b["status"] == "VERIFIED", "BENEFICIARY_UNAVAILABLE")
            self.cash(c, -n)
            payment = dict(amount_minor=n, beneficiary_ref=b["reference"], minute=self.w["minute"])
            o["paid_minor"] += n
            o["payments"].append(payment)
            receipt = dict(obligation_ref=o["reference"], **payment)
        elif action == "send_net":
            self.args(data, "order_id", "beneficiary_id", "bank_version", "net_minor")
            o = self.local(c, data["order_id"], "net_delivery_order")
            b = self.local(c, data["beneficiary_id"], "factoring_beneficiary")
            terms = self.byref(c, "REMITTANCE_TERMS")
            n = data["net_minor"]
            self.positive(n)
            self.check(
                self.w["minute"] <= o["advice_due_minute"], "ORIGINAL_SUBMISSION_WINDOW_CLOSED"
            )
            self.check(
                b["status"] == "VERIFIED" and b["bank_version"] == data["bank_version"],
                "BANK_VERSION_UNAVAILABLE",
            )
            self.cash(c, -n - terms["fee_hold_minor"])
            payment = dict(
                number=len(o["instructions"]) + 1,
                net_minor=n,
                fee_hold_minor=terms["fee_hold_minor"],
                beneficiary_ref=b["reference"],
                bank_version=data["bank_version"],
                submitted_minute=self.w["minute"],
                status="SENT",
                delivered_minor=0,
                return_principal_minor=0,
                fee_minor=0,
                return_fee_minor=0,
            )
            o["instructions"].append(payment)
            receipt = dict(order_ref=o["reference"], instruction=deepcopy(payment))
        elif action == "repay_margin":
            self.args(data, "invoice_id", "amount_minor")
            inv = self.local(c, data["invoice_id"], "factoring_invoice")
            n = data["amount_minor"]
            self.positive(n)
            self.check(n <= inv["margin_due_minor"], "MARGIN_AMOUNT_EXCEEDED")
            self.cash(c, -n)
            inv["margin_due_minor"] -= n
            inv["advance_repaid_minor"] += n
            receipt = dict(
                invoice_ref=inv["reference"],
                amount_minor=n,
                remaining_margin_minor=inv["margin_due_minor"],
            )
        elif action == "topup":
            self.args(data, "order_id", "beneficiary_id", "bank_version", "amount_minor")
            o = self.local(c, data["order_id"], "net_delivery_order")
            b = self.local(c, data["beneficiary_id"], "factoring_beneficiary")
            terms = self.byref(c, "REMITTANCE_TERMS")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                self.w["minute"] >= o["return_minute"]
                and all(p["status"] in ("DELIVERED", "RETURNED") for p in o["instructions"]),
                "RETURN_NOT_AVAILABLE",
            )
            self.check(
                b["status"] == "VERIFIED" and b["bank_version"] == data["bank_version"],
                "BANK_VERSION_UNAVAILABLE",
            )
            self.cash(c, -n)
            self.charge(c, terms["topup_fee_minor"])
            c["state"]["remittance_fees_minor"] += terms["topup_fee_minor"]
            o["paid_minor"] += n
            payment = dict(
                amount_minor=n,
                beneficiary_ref=b["reference"],
                bank_version=data["bank_version"],
                minute=self.w["minute"],
            )
            o["topups"].append(payment)
            receipt = dict(order_ref=o["reference"], **payment)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="priority_factoring",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=c["fees_minor"] - beforefees,
            cash_after_minor=c["cash_minor"],
        )

    def factoring_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        kind = self.w["_event_payloads"][event["id"]]["event_kind"]
        before = c["cash_minor"]
        fees = c["fees_minor"]
        receipt = {}
        if kind == "notice_confirmation":
            refs = []
            for inv in self.records(c, "factoring_invoice"):
                if inv["notice_status"] == "SENT":
                    inv["notice_status"] = "CONFIRMED"
                    refs.append(inv["reference"])
            receipt = dict(confirmed_invoice_refs=refs)
        elif kind == "credits_and_delivery":
            credits = []
            margins = []
            for note in self.records(c, "buyer_credit_note"):
                if note["status"] == "PENDING":
                    note.update(status="FINAL", amount_minor=note["_actual_amount_minor"])
                    credits.append(
                        dict(note_ref=note["reference"], amount_minor=note["amount_minor"])
                    )
            for inv in self.records(c, "factoring_invoice"):
                if inv["assigned"]:
                    offer = self.byref(c, inv["factor_offer_ref"])
                    limit = self.factoring_face(c, inv) * offer["advance_bp"] // 10000
                    due = max(0, inv["advance_minor"] - inv["advance_repaid_minor"] - limit)
                    inv.update(
                        margin_due_minor=due, margin_due_minute=self.w["minute"] if due else None
                    )
                    margins.append(
                        dict(
                            invoice_ref=inv["reference"],
                            permitted_advance_minor=limit,
                            margin_due_minor=due,
                        )
                    )
            terms = self.byref(c, "REMITTANCE_TERMS")
            deliveries = []
            for order in self.records(c, "net_delivery_order"):
                for p in order["instructions"]:
                    delivered = min(p["net_minor"], order["_actual_first_delivery_minor"])
                    fee = terms["_actual_fee_minor"]
                    self.check(fee <= p["fee_hold_minor"], "FEE_HOLD_INCONSISTENT")
                    self.cash(c, p["fee_hold_minor"] - fee)
                    c["fees_minor"] += fee
                    c["state"]["remittance_fees_minor"] += fee
                    p.update(
                        status="DELIVERED" if delivered == p["net_minor"] else "RETURN_IN_TRANSIT",
                        delivered_minor=delivered,
                        return_principal_minor=p["net_minor"] - delivered,
                        fee_minor=fee,
                    )
                    order["paid_minor"] += delivered
                    deliveries.append(
                        dict(
                            order_ref=order["reference"],
                            number=p["number"],
                            delivered_minor=delivered,
                            fee_minor=fee,
                            unused_fee_returned_minor=p["fee_hold_minor"] - fee,
                            return_principal_minor=p["return_principal_minor"],
                        )
                    )
                self.byref(c, order["beneficiary_ref"])["bank_version"] = terms["new_bank_version"]
            receipt = dict(
                credits=credits,
                margins=margins,
                deliveries=deliveries,
                new_supplier_bank_version=terms["new_bank_version"],
            )
        elif kind == "principal_returns":
            terms = self.byref(c, "REMITTANCE_TERMS")
            returns = []
            for order in self.records(c, "net_delivery_order"):
                for p in order["instructions"]:
                    if p["status"] == "RETURN_IN_TRANSIT":
                        gross = p["return_principal_minor"]
                        charge = min(gross, terms["return_fee_minor"])
                        self.cash(c, gross - charge)
                        c["fees_minor"] += charge
                        c["state"]["remittance_fees_minor"] += charge
                        p.update(
                            status="RETURNED", return_principal_minor=0, return_fee_minor=charge
                        )
                        returns.append(
                            dict(
                                order_ref=order["reference"],
                                number=p["number"],
                                gross_minor=gross,
                                charge_minor=charge,
                                net_wallet_minor=gross - charge,
                            )
                        )
            receipt = dict(returns=returns)
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=c["fees_minor"] - fees,
            cash_after_minor=c["cash_minor"],
        )
