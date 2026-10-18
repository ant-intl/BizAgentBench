from copy import deepcopy


class EscrowOperations:
    def escrow_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "inspection_escrow", "SERVICE_CONTRACT_MISMATCH")
        self.args(data, "lot_id", "recipient_id", "amount_minor")
        lot = self.local(c, data["lot_id"], "escrow_lot")
        party = self.local(c, data["recipient_id"], "escrow_party")
        n = data["amount_minor"]
        self.positive(n)
        ref = party["reference"]
        cash = c["cash_minor"]
        out = {}
        if action in ("distribute", "refund_buyer"):
            self.check(lot["available_minor"] >= n, "INSUFFICIENT_LOT_CASH")
            lot["available_minor"] -= n
            self.cash(c, -n)
            key = "distributions_minor" if action == "distribute" else "refunds_minor"
            lot[key][ref] = lot[key].get(ref, 0) + n
        elif action == "return_distribution":
            pending = sum(
                p["amount_minor"]
                for eid, p in self.w["_event_payloads"].items()
                if p.get("event_kind") == "distribution_return"
                and p["lot_ref"] == lot["reference"]
                and p["party_ref"] == ref
                and self.w["events"][eid]["status"] == "SCHEDULED"
            )
            self.check(
                n
                <= lot["distributions_minor"].get(ref, 0)
                - lot["returned_minor"].get(ref, 0)
                - pending,
                "EXCEEDS_OUTSTANDING_DISTRIBUTION",
            )
            eid = c["scope"] + "-RETURN-" + str(len(self.w["events"]))
            self.w["events"][eid] = dict(
                id=eid,
                scope=c["scope"],
                kind="escrow_event",
                event_category="distribution_return",
                target_id=case_id,
                due_minute=self.w["minute"] + 1,
                status="SCHEDULED",
            )
            self.w["_event_payloads"][eid] = dict(
                event_kind="distribution_return",
                lot_ref=lot["reference"],
                party_ref=ref,
                amount_minor=n,
            )
            out = dict(event_id=eid, due_minute=self.w["minute"] + 1)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="inspection_escrow",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            receipt=out,
        )

    def escrow_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        if p["event_kind"] == "final_inspections":
            for z in self.records(c, "acceptance_report"):
                if z["status"] == "PENDING":
                    z.update(
                        accepted_prices_minor=deepcopy(z["_actual_prices_minor"]),
                        status=z["_actual_status"],
                        issued_minute=self.w["minute"],
                    )
        elif p["event_kind"] == "distribution_return":
            lot = self.byref(c, p["lot_ref"])
            party = p["party_ref"]
            n = p["amount_minor"]
            lot["available_minor"] += n
            self.cash(c, n)
            lot["returned_minor"][party] = lot["returned_minor"].get(party, 0) + n
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            receipt=deepcopy(p),
        )
