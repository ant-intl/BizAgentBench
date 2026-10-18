from copy import deepcopy


class EvidencePeriodsOperations:
    def evidence_periods_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "period_coverage", "SERVICE_ACTION_MISMATCH")
        cash = c["cash_minor"]
        out = {}
        if action == "settle":
            self.args(data, "loss_id", "amount_minor", "evidence_ids")
            z = self.local(c, data["loss_id"], "insured_loss")
            n = data["amount_minor"]
            self.check(
                type(n) is int
                and 0 <= n <= z["maximum_compromise_minor"]
                and z["status"] == "OPEN",
                "FINAL_COMPROMISE_UNAVAILABLE",
            )
            ee = data["evidence_ids"]
            self.check(
                type(ee) is list and len(ee) > 0 and len(set(ee)) == len(ee), "EVIDENCE_REQUIRED"
            )
            docs = [self.local(c, i, "ownership_evidence") for i in ee]
            self.cash(c, n - z["advance_minor"])
            z.update(
                final_indemnity_minor=n,
                status="SETTLED",
                evidence_refs=[d["reference"] for d in docs],
            )
            out = dict(
                loss_ref=z["reference"],
                final_indemnity_minor=n,
                advance_difference_minor=n - z["advance_minor"],
            )
        elif action == "pay":
            self.args(data, "bill_id", "amount_minor")
            z = self.local(c, data["bill_id"], "replacement_bill")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            z["paid_minor"] += n
            out = dict(bill_ref=z["reference"], paid_minor=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="period_coverage",
            arguments=deepcopy(data),
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )

    def evidence_periods_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        a = self.byref(c, "PRESS")
        p = a["_final_transfer_period"]
        a.update(effective_until_exclusive=p, registry_status="FINAL")
        self.byref(c, "PRESS_BUYER").update(effective_from=p, registry_status="FINAL")
        self.byref(c, "PRESS_CERT").update(period_until_exclusive=p, status="SIGNED")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind="ownership_confirmation",
            receipt=dict(asset_ref="PRESS", transfer_period=p),
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
        )
