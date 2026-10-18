from copy import deepcopy


class GuaranteedNetOperations:
    def guaranteed_net_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "contingent_net", "SERVICE_ACTION_MISMATCH")
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        out = {}
        if action == "send":
            self.args(data, "invoice_id", "beneficiary_id", "route", "amount_minor")
            z = self.local(c, data["invoice_id"], "net_invoice")
            b = self.local(c, data["beneficiary_id"], "net_beneficiary")
            n = data["amount_minor"]
            self.positive(n)
            route = data["route"]
            if route == "protected":
                g = self.byref(c, "GUARANTEE")
                self.check(
                    g["used_for"] is None
                    and self.w["minute"] <= g["available_through"]
                    and n <= g["maximum_net_minor"],
                    "NET_CERTIFICATE_UNAVAILABLE",
                )
                self.cash(c, -n)
                self.charge(c, g["fee_minor"])
                g["used_for"] = z["reference"]
                z["received_minor"] += n
                out = dict(
                    invoice_ref=z["reference"],
                    beneficiary_ref=b["reference"],
                    net_minor=n,
                    route=route,
                )
            elif route == "standard":
                s = c["state"].setdefault("standard", {})
                self.check(
                    self.w["minute"] < self.byref(c, "ROUTES")["standard_result_minute"]
                    and z["reference"] not in s
                    and n > max(z["possible_standard_fees_minor"]),
                    "STANDARD_DELIVERY_UNAVAILABLE",
                )
                self.cash(c, -n)
                s[z["reference"]] = dict(
                    gross_minor=n,
                    beneficiary_ref=b["reference"],
                    status="SENT",
                    actual_fee_minor=None,
                    actual_net_minor=None,
                )
                out = dict(
                    invoice_ref=z["reference"],
                    beneficiary_ref=b["reference"],
                    gross_minor=n,
                    route=route,
                )
            else:
                self.check(False, "UNKNOWN_ROUTE")
        elif action == "topup":
            self.args(data, "invoice_id", "beneficiary_id", "net_minor")
            z = self.local(c, data["invoice_id"], "net_invoice")
            b = self.local(c, data["beneficiary_id"], "net_beneficiary")
            n = data["net_minor"]
            self.positive(n)
            s = c["state"].get("standard", {}).get(z["reference"])
            self.check(s and s["status"] == "FINAL", "STANDARD_RESULT_REQUIRED")
            self.cash(c, -n)
            self.charge(c, self.byref(c, "ROUTES")["topup_fee_minor"])
            z["received_minor"] += n
            out = dict(invoice_ref=z["reference"], beneficiary_ref=b["reference"], net_minor=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="contingent_net",
            arguments=deepcopy(data),
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )

    def guaranteed_net_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        out = {}
        if p["event_kind"] == "old_transfer_result":
            z = self.byref(c, "OLD_TRANSFER")
            n = z["_credited_minor"]
            ret = z["gross_minor"] - n - z["return_fee_minor"]
            z.update(status="FINAL", actual_credited_minor=n, actual_returned_minor=ret)
            self.byref(c, z["invoice_ref"])["received_minor"] += n
            self.cash(c, ret)
            c["fees_minor"] += z["return_fee_minor"]
            d = self.byref(c, "DIRECTION")
            self.byref(c, d["invoice_ref"])["current_beneficiary_ref"] = d["beneficiary_ref"]
            out = dict(
                beneficiary_ref=z["beneficiary_ref"],
                credited_minor=n,
                returned_minor=ret,
                return_fee_minor=z["return_fee_minor"],
            )
        else:
            for ref, s in c["state"].get("standard", {}).items():
                z = self.byref(c, ref)
                fee = z["_standard_fee_minor"]
                net = s["gross_minor"] - fee
                z["received_minor"] += net
                c["fees_minor"] += fee
                s.update(status="FINAL", actual_fee_minor=fee, actual_net_minor=net)
                out[ref] = dict(
                    beneficiary_ref=s["beneficiary_ref"],
                    gross_minor=s["gross_minor"],
                    fee_minor=fee,
                    net_minor=net,
                )
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=p["event_kind"],
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )
