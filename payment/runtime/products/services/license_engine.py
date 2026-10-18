from copy import deepcopy


class LicenseOperations:
    def license_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(
            c["kind"] == "source_bound_licenses" and action == "ship", "SERVICE_ACTION_MISMATCH"
        )
        self.args(data, "shipment_id", "permit_id", "quantity_units")
        s = self.local(c, data["shipment_id"], "licensed_shipment")
        p = self.local(c, data["permit_id"], "shipment_permit")
        n = data["quantity_units"]
        self.positive(n)
        self.check(
            p["status"] == "ISSUED" and p["valid_from"] <= self.w["minute"] <= p["valid_through"],
            "PERMIT_NOT_CURRENT",
        )
        self.check(
            s["exporter"] == p["exporter"]
            and s["material"] in p["materials"]
            and s["destination"] in p["destinations"],
            "OUTSIDE_PERMIT_SCOPE",
        )
        self.check(
            self.w["minute"] >= s["stock_ready_minute"]
            and (s["stock_ready_minute"] == 0 or c["state"].get("stock_delivered")),
            "STOCK_NOT_AVAILABLE",
        )
        self.check(
            n <= s["quantity_units"] - s["shipped_units"]
            and n <= p["capacity_units"] - p["used_units"],
            "INSUFFICIENT_SHIPMENT_OR_PERMIT_QUANTITY",
        )
        cash = c["cash_minor"]
        fee = n * p["premium_fee_minor_per_unit"]
        self.charge(c, fee)
        s["shipped_units"] += n
        p["used_units"] += n
        return self.book(
            c,
            "ship",
            contract="source_bound_licenses",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=fee,
        )

    def license_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        out = {}
        if p["event_kind"] == "quota_return":
            old = self.byref(c, p["reference"])
            permit = self.byref(c, old["original_permit_ref"])
            n = old["_actual_return_units"]
            old.update(returned_units=n, status="RETURN_CONFIRMED")
            permit["used_units"] -= n
            out = dict(original_permit_ref=permit["reference"], actual_return_units=n)
        elif p["event_kind"] == "permit_issue":
            permit = self.byref(c, p["reference"])
            permit.update(capacity_units=permit["_actual_issue_units"], status="ISSUED")
            out = dict(actual_capacity_units=permit["capacity_units"])
        elif p["event_kind"] == "stock_delivery":
            c["state"]["stock_delivered"] = True
            out = dict(stock_delivered=True)
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
        )
