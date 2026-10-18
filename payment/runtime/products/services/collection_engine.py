from copy import deepcopy


class CollectionOperations:
    def collection_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "actual_collections", "SERVICE_ACTION_MISMATCH")
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        out = {}
        if action == "discount":
            self.args(data, "bill_id", "amount_minor")
            b = self.local(c, data["bill_id"], "collection_bill")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                b["status"] == "ACCEPTED"
                and not b["discount_used"]
                and n <= b["accepted_minor"] - b["received_minor"] - b["assigned_outstanding_minor"]
                and n > b["discount_fee_minor"],
                "RECEIVABLE_UNAVAILABLE",
            )
            self.cash(c, n)
            self.charge(c, b["discount_fee_minor"])
            b.update(discount_used=True, discounted_minor=n, assigned_outstanding_minor=n)
            out = dict(bill_ref=b["reference"], assigned_minor=n)
        elif action == "pay":
            self.args(data, "payable_id", "amount_minor")
            p = self.local(c, data["payable_id"], "payable")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            p["paid_minor"] += n
            out = dict(payable_ref=p["reference"], paid_minor=n)
        elif action in ("deliver", "return_documents"):
            self.args(data, "bill_id", "quantity_units")
            b = self.local(c, data["bill_id"], "collection_bill")
            n = data["quantity_units"]
            self.positive(n)
            self.check(
                n + b["delivered_units"] + b["returned_units"]
                <= b["face_minor"] // b["unit_price_minor"],
                "DOCUMENT_QUANTITY_UNAVAILABLE",
            )
            b["delivered_units" if action == "deliver" else "returned_units"] += n
            out = dict(bill_ref=b["reference"], quantity_units=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="actual_collections",
            arguments=deepcopy(data),
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )

    def collection_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        out = {}
        terms = self.byref(c, "TERMS")
        for b in self.records(c, "collection_bill"):
            if p["event_kind"] == "acceptance":
                b.update(accepted_minor=b["_accepted_minor"], status="ACCEPTED")
                out[b["reference"]] = dict(accepted_minor=b["accepted_minor"])
            else:
                n = (
                    terms["early_receipts"][b["reference"]]
                    if p["event_kind"] == "early_receipt"
                    else b["accepted_minor"] - b["received_minor"]
                )
                assigned = min(n, b["assigned_outstanding_minor"])
                b["received_minor"] += n
                b["assigned_outstanding_minor"] -= assigned
                self.cash(c, n - assigned)
                out[b["reference"]] = dict(
                    buyer_paid_minor=n,
                    assigned_discharge_minor=assigned,
                    cash_credit_minor=n - assigned,
                )
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )
