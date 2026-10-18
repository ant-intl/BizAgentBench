from copy import deepcopy


class CreditOperations:
    def credit_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "documentary_imports", "SERVICE_ACTION_MISMATCH")
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        out = {}
        if action == "direct_pay":
            self.args(data, "order_id", "beneficiary_id", "amount_minor")
            o = self.local(c, data["order_id"], "import_order")
            b = self.local(c, data["beneficiary_id"], "beneficiary")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            out = dict(order_ref=o["reference"], beneficiary_ref=b["reference"], paid_minor=n)
        elif action == "recall_drawing":
            self.args(data, "drawing_id")
            d = self.local(c, data["drawing_id"], "documentary_drawing")
            self.check(d["status"] == "ACCEPTED", "DRAWING_NOT_RECALLABLE")
            d["status"] = "RECALL_REQUESTED"
        elif action == "settle_confirmed":
            self.args(data, "drawing_id")
            d = self.local(c, data["drawing_id"], "documentary_drawing")
            self.check(d["status"] == "CONFIRMED_UNPAID", "NO_UNPAID_CONFIRMATION")
            self.cash(c, -d["actual_minor"])
            d["status"] = "FINAL"
            out = dict(
                order_ref=d["order_ref"],
                beneficiary_ref=d["beneficiary_ref"],
                paid_minor=d["actual_minor"],
            )
        elif action == "issue_guarantee":
            self.args(data, "order_id")
            o = self.local(c, data["order_id"], "import_order")
            title = self.byref(c, o["title_ref"])
            terms = self.byref(c, "GUARANTEE_TERMS")
            self.check(
                title["custody"] == "BANK"
                and o["reference"] not in c["state"].get("guarantees", {}),
                "GUARANTEE_UNAVAILABLE",
            )
            self.charge(c, terms["fee_minor"])
            self.cash(c, -terms["collateral_minor"])
            c["state"].setdefault("guarantees", {})[o["reference"]] = dict(
                status="ACTIVE",
                collateral_minor=terms["collateral_minor"],
                issued_minute=self.w["minute"],
                returned_minute=None,
            )
        elif action == "lodge_originals":
            self.args(data, "title_id")
            title = self.local(c, data["title_id"], "original_title")
            self.check(title["custody"] == "BANK", "TITLE_NOT_IN_BANK")
            self.check(
                all(
                    d["status"] in ("SETTLED", "RETURNED", "FINAL")
                    for d in self.records(c, "documentary_drawing")
                    if d["title_ref"] == title["reference"]
                ),
                "ORIGINALS_RETAINED",
            )
            title.update(custody="CARRIER", lodged_minute=self.w["minute"])
            for ref, g in c["state"].get("guarantees", {}).items():
                if (
                    self.byref(c, ref)["title_ref"] == title["reference"]
                    and g["status"] == "ACTIVE"
                ):
                    self.cash(c, g["collateral_minor"])
                    g.update(status="RETURNED", returned_minute=self.w["minute"])
        elif action == "deliver":
            self.args(data, "order_id", "depot_id", "quantity_units")
            o = self.local(c, data["order_id"], "import_order")
            depot = self.local(c, data["depot_id"], "delivery_depot")
            n = data["quantity_units"]
            self.positive(n)
            self.check(n <= o["quantity_units"] - o["delivered_units"], "CARGO_QUANTITY_EXCEEDED")
            g = c["state"].get("guarantees", {}).get(o["reference"])
            self.check(
                self.byref(c, o["title_ref"])["custody"] == "CARRIER"
                or (g and g["status"] == "ACTIVE"),
                "CARGO_TITLE_UNAVAILABLE",
            )
            o["delivered_units"] += n
            out = dict(order_ref=o["reference"], depot_ref=depot["reference"], quantity_units=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="documentary_imports",
            arguments=deepcopy(data),
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )

    def credit_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        d = self.byref(c, p["drawing_ref"])
        self.check(d["status"] in ("ACCEPTED", "RECALL_REQUESTED"), "DRAWING_NOT_PENDING")
        n = d["_recalled_actual_minor"] if d["status"] == "RECALL_REQUESTED" else d["_actual_minor"]
        d["actual_minor"] = n
        paid = c["cash_minor"] >= n
        if paid:
            self.cash(c, -n)
        d["status"] = "FINAL" if paid else "CONFIRMED_UNPAID"
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            receipt=dict(
                drawing_ref=d["reference"],
                actual_minor=n,
                paid=paid,
                beneficiary_ref=d["beneficiary_ref"],
            ),
            cash_delta_minor=-n if paid else 0,
            cash_after_minor=c["cash_minor"],
        )
