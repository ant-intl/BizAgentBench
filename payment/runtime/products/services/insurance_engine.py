from copy import deepcopy


class InsuranceOperations:
    def insurance_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "shared_indemnity", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        if action == "sell_salvage":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "insured_loss")
            self.check(
                not r["salvage_realized"] and r["salvage_minor"] > 0, "SALVAGE_NOT_AVAILABLE"
            )
            r["salvage_realized"] = True
            self.cash(c, r["salvage_minor"])
        elif action in ("claim", "return_indemnity"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "insurance_position")
            n = data["amount_minor"]
            self.positive(n)
            policy = self.byref(c, r["policy"])
            if action == "claim":
                held = sum(
                    p["requested_minor"] - p["returned_minor"]
                    for p in self.records(c, "insurance_position")
                    if p["policy"] == r["policy"]
                )
                self.check(
                    r["requested_minor"] - r["returned_minor"] + n <= policy["per_loss_cap_minor"]
                    and held + n <= policy["aggregate_cap_minor"],
                    "POLICY_ADVANCE_LIMIT_EXCEEDED",
                )
                r["requested_minor"] += n
                self.schedule(
                    "insurance_event",
                    case_id,
                    policy["payout_delay"],
                    event_kind="indemnity_payout",
                    reference=r["reference"],
                    amount_minor=n,
                )
            else:
                self.check(
                    n <= r["paid_minor"] - r["returned_minor"], "RETURN_EXCEEDS_RECEIVED_INDEMNITY"
                )
                self.cash(c, -n)
                r["returned_minor"] += n
        elif action == "pay_replacement":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "replacement_bill")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["paid_minor"] += data["amount_minor"]
        elif action == "record_recovery":
            self.args(data, "record_id", "external_recovery_minor")
            r = self.local(c, data["record_id"], "insured_loss")
            self.nonnegative(data["external_recovery_minor"])
            r.update(
                rights_reconciled=True,
                declared_external_recovery_minor=data["external_recovery_minor"],
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="shared_indemnity",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def insurance_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        kind = p["event_kind"]
        out = {}
        if kind == "merchant_recovery":
            r = self.byref(c, p["reference"])
            r["merchant_received_minor"] += r["_merchant_actual_minor"]
            self.cash(c, r["_merchant_actual_minor"])
            out = dict(reference=r["reference"], received_minor=r["_merchant_actual_minor"])
        elif kind == "indemnity_payout":
            r = self.byref(c, p["reference"])
            r["paid_minor"] += p["amount_minor"]
            self.cash(c, p["amount_minor"])
            out = dict(reference=r["reference"], paid_minor=p["amount_minor"])
        elif kind != "marker":
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
