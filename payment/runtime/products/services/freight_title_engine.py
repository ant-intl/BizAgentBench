from copy import deepcopy


class FreightTitleOperations:
    def _fee(self, case, amount):
        self.check(amount <= case["cash_minor"], "INSUFFICIENT_RELEASE_CASH")
        case["cash_minor"] -= amount
        case["fees_minor"] += amount

    def freight_title_instruction(self, case_id, action, data):
        case = self.row("service_cases", case_id)
        self.check(case["kind"] == "document_title_allocation", "SERVICE_CONTRACT_MISMATCH")
        before = case["cash_minor"]
        terms = self.byref(case, "TERMS")
        if action == "activate_split":
            self.args(data, "title_id")
            title = self.local(case, data["title_id"], "title_document")
            self.check(
                title["status"] == "ACTIVE"
                and title["endorsed_destination_ref"] is None
                and title["split_child_refs"],
                "TITLE_NOT_SPLITTABLE",
            )
            children = [self.byref(case, ref) for ref in title["split_child_refs"]]
            self.check(
                all(x["status"] == "PENDING_SPLIT" for x in children), "SPLIT_CHILD_NOT_PENDING"
            )
            self._fee(case, terms["split_fee_minor"])
            title["status"] = "RETIRED_BY_SPLIT"
            for child in children:
                child["status"] = "ACTIVE"
            receipt = dict(
                retired_title_ref=title["reference"],
                activated_title_refs=[x["reference"] for x in children],
            )
        elif action == "endorse_title":
            self.args(data, "title_id", "destination_id")
            title = self.local(case, data["title_id"], "title_document")
            destination = self.local(case, data["destination_id"], "cargo_destination")
            self.check(
                title["status"] == "ACTIVE" and title["endorsed_destination_ref"] is None,
                "TITLE_NOT_ENDORSABLE",
            )
            self._fee(case, terms["endorsement_fee_minor"])
            title["endorsed_destination_ref"] = destination["reference"]
            receipt = dict(
                title_ref=title["reference"],
                destination_ref=destination["reference"],
                covers=deepcopy(title["covers"]),
            )
        elif action == "dispatch":
            self.args(data, "lot_id", "window_id", "destination_id")
            lot = self.local(case, data["lot_id"], "titled_cargo_lot")
            window = self.local(case, data["window_id"], "pickup_window")
            destination = self.local(case, data["destination_id"], "cargo_destination")
            self.check(
                not lot["dispatched"]
                and window["status"] == "OPEN"
                and window["minute"] == self.w["minute"],
                "LOT_OR_WINDOW_NOT_AVAILABLE",
            )
            self.check(
                lot["handling_units"] <= window["capacity_units"] - window["used_units"],
                "INSUFFICIENT_PICKUP_CAPACITY",
            )
            titles = [
                x
                for x in self.records(case, "title_document")
                if x["status"] == "ACTIVE"
                and lot["reference"] in x["covers"]
                and x["endorsed_destination_ref"] == destination["reference"]
            ]
            self.check(bool(titles), "NO_ENDORSED_CURRENT_TITLE")
            tariff = self.byref(case, lot["tariff_ref"])
            d = max(0, self.w["minute"] - tariff["free_through"])
            storage = lot["handling_units"] * (
                min(d, 1) * tariff["first_window_rate_minor"]
                + max(0, d - 1) * tariff["later_window_rate_minor"]
            )
            loading = tariff["loading_fees_minor"][str(self.w["minute"])]
            self._fee(case, storage + loading)
            window["used_units"] += lot["handling_units"]
            lot.update(
                dispatched=True,
                dispatched_minute=self.w["minute"],
                actual_destination_ref=destination["reference"],
                title_ref=titles[0]["reference"],
            )
            receipt = dict(
                lot_ref=lot["reference"],
                destination_ref=destination["reference"],
                title_ref=titles[0]["reference"],
                handling_units=lot["handling_units"],
                storage_minor=storage,
                loading_minor=loading,
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            case,
            action,
            contract="document_title_allocation",
            arguments=deepcopy(data),
            receipt=receipt,
            fee_minor=before - case["cash_minor"],
            cash_delta_minor=case["cash_minor"] - before,
            cash_after_minor=case["cash_minor"],
        )

    def freight_title_event(self, event):
        case = self.w["service_cases"][event["target_id"]]
        payload = self.w["_event_payloads"][event["id"]]
        self.check(payload["event_kind"] == "window_opening", "UNKNOWN_EVENT")
        for row in self.records(case, "pickup_window"):
            if row["status"] == "OPEN":
                row["status"] = "CLOSED"
        window = self.byref(case, payload["window_ref"])
        window["status"] = "OPEN"
        case["state"]["active_window_ref"] = window["reference"]
        return self.book(
            case,
            "bank_event",
            event_kind="window_opening",
            event_id=event["id"],
            receipt=dict(opened_window_ref=window["reference"]),
            fee_minor=0,
            cash_delta_minor=0,
            cash_after_minor=case["cash_minor"],
        )
