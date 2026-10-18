from copy import deepcopy


class HandlingOperations:
    def handling_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(
            c["kind"] == "inspected_handling" and action == "dispatch", "SERVICE_ACTION_MISMATCH"
        )
        self.args(data, "lot_id", "shift_id", "destination_id")
        lot = self.local(c, data["lot_id"], "sealed_cargo_lot")
        shift = self.local(c, data["shift_id"], "handling_shift")
        dest = self.local(c, data["destination_id"], "cargo_destination")
        self.check(
            lot["status"] == "RELEASED"
            and not lot["dispatched"]
            and self.w["minute"] >= lot["ready_minute"],
            "LOT_NOT_AVAILABLE",
        )
        self.check(
            shift["status"] == "OPEN" and shift["minute"] == self.w["minute"], "WINDOW_NOT_CURRENT"
        )
        self.check(dest["route"] in shift["accepted_routes"], "ROUTE_NOT_AVAILABLE")
        n = lot["handling_units"]
        self.check(
            n <= shift["capacity_units"] - shift["used_units"], "INSUFFICIENT_HANDLING_CAPACITY"
        )
        tariff = self.byref(c, lot["tariff_ref"])
        d = max(0, self.w["minute"] - tariff["free_through"])
        storage = n * (
            min(d, 1) * tariff["first_window_rate_minor"]
            + max(0, d - 1) * tariff["later_window_rate_minor"]
        )
        loading = tariff["loading_fees_minor"][str(self.w["minute"])]
        fee = storage + loading
        self.charge(c, fee)
        shift["used_units"] += n
        lot.update(
            dispatched=True,
            dispatched_minute=self.w["minute"],
            actual_destination_ref=dest["reference"],
        )
        return self.book(
            c,
            "dispatch",
            contract="inspected_handling",
            arguments=deepcopy(data),
            receipt=dict(
                lot_ref=lot["reference"],
                handling_units=n,
                destination_ref=dest["reference"],
                storage_minor=storage,
                loading_minor=loading,
            ),
            fee_minor=fee,
            cash_delta_minor=-fee,
            cash_after_minor=c["cash_minor"],
        )

    def handling_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        out = dict(opened_shift_ref=p["shift_ref"])
        if p["event_kind"] == "inspection_and_opening":
            inspection = self.byref(c, p["inspection_ref"])
            n = inspection["_actual_accepted_units"]
            inspection.update(status="FINAL", actual_accepted_units=n)
            for ref, size in [
                (inspection["accepted_lot_ref"], n),
                (inspection["rejected_lot_ref"], inspection["total_handling_units"] - n),
            ]:
                self.byref(c, ref).update(status="RELEASED", handling_units=size)
            out.update(accepted_units=n, rejected_units=inspection["total_handling_units"] - n)
        for shift in self.records(c, "handling_shift"):
            if shift["status"] == "OPEN":
                shift["status"] = "CLOSED"
        self.byref(c, p["shift_ref"])["status"] = "OPEN"
        c["state"]["active_shift_ref"] = p["shift_ref"]
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
        )
