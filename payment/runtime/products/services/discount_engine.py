from copy import deepcopy


class DiscountOperations:
    def discount_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "dated_discount_credit", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        now = self.w["minute"]
        if action == "settle_invoice":
            self.args(data, "invoice_id", "amount_minor")
            r = self.local(c, data["invoice_id"], "discount_payable")
            self.positive(data["amount_minor"])
            self.check(
                r["status"] == "PAYABLE" and not r["settled"] and now >= r["earliest_minute"],
                "INVOICE_NOT_PAYABLE",
            )
            due = r["face_minor"] - r["historical_paid_minor"] - r["applied_credit_minor"]
            discount = due * r["discount_bp"] // 10000 if now <= r["discount_through"] else 0
            amount = due - discount
            self.check(data["amount_minor"] == amount, "INCORRECT_SETTLEMENT_AMOUNT")
            self.check(c["cash_minor"] >= amount, "INSUFFICIENT_PAYABLE_CASH")
            c["cash_minor"] -= amount
            r.update(settled=True, paid_minor=amount, settled_minute=now)
            receipt = dict(
                invoice_ref=r["reference"],
                remaining_face_minor=due,
                discount_minor=discount,
                paid_minor=amount,
            )
        elif action == "release_stock":
            self.args(data, "order_id", "destination_id")
            r = self.local(c, data["order_id"], "stock_credit_order")
            dest = self.local(c, data["destination_id"], "stock_destination")
            supplier = self.byref(c, r["supplier_ref"])
            self.check(not r["released"], "STOCK_ALREADY_RELEASED")
            exposure = (
                sum(
                    x["face_minor"] - x["historical_paid_minor"] - x["applied_credit_minor"]
                    for x in self.records(c, "discount_payable")
                    if x["supplier_ref"] == r["supplier_ref"] and not x["settled"]
                )
                + supplier["stock_hold_minor"]
                + r["credit_hold_minor"]
            )
            self.check(exposure <= supplier["limit_minor"], "SUPPLIER_CREDIT_LIMIT")
            supplier["stock_hold_minor"] += r["credit_hold_minor"]
            r.update(released=True, released_minute=now, actual_destination_ref=dest["reference"])
            receipt = dict(
                order_ref=r["reference"],
                destination_ref=dest["reference"],
                supplier_ref=supplier["reference"],
                exposure_minor=exposure,
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="dated_discount_credit",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )

    def discount_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        bank = self.byref(c, p["receipt_ref"])
        before = c["cash_minor"]
        self.check(bank["status"] == "SCHEDULED", "RECEIPT_ALREADY_POSTED")
        bank["status"] = "POSTED"
        c["cash_minor"] += bank["amount_minor"]
        notice = None
        if p["review_ref"]:
            n = self.byref(c, p["review_ref"])
            invoice = self.byref(c, n["invoice_ref"])
            credit = n["_actual_credit_minor"]
            self.check(credit in n["possible_credit_minor"], "INVALID_REVIEW_OUTCOME")
            n.update(status="FINAL", actual_credit_minor=credit)
            invoice.update(applied_credit_minor=credit, status="PAYABLE")
            notice = dict(invoice_ref=invoice["reference"], credit_minor=credit)
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(
                receipt_ref=bank["reference"], amount_minor=bank["amount_minor"], credit_note=notice
            ),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
