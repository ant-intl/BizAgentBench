"""Shared release scheduling and version semantics, independent of task IDs."""

from copy import deepcopy


class CustomerConstraints:
    def public(self, r, resource=None):
        out = self.next_handler(CustomerConstraints).public(r, resource)
        if "reserve_account_id" in out and "conditions" in out:
            from payment.runtime.products.constraints.public_terms import clauses

            out["release_terms"] = clauses(out.pop("conditions"))
            if "assessment_conditions" in out:
                out["assessment_terms"] = clauses(out.pop("assessment_conditions"))
        return out

    def call_discoverable_agent_tool(self, capability_name, arguments):
        out = self.next_handler(CustomerConstraints).call_discoverable_agent_tool(
            capability_name, arguments
        )
        if capability_name == "propose_reserve_disbursement":
            r = self.row("requests", out["id"])
            r["reserve_terms_version"] = self.row("reserve_routes", arguments["route_id"]).get(
                "terms_version", 1
            )
            out = self.public(r)
        return out

    def execute_request(self, request_id):
        r = self.row("requests", request_id)
        if r["operation"] == "propose_reserve_disbursement" and r["status"] != "EXECUTED":
            self.check(
                r["reserve_terms_version"]
                == self.row("reserve_routes", r["arguments"]["route_id"]).get("terms_version", 1),
                "RESERVE_TERMS_VERSION_CHANGED",
            )
        result = self.next_handler(CustomerConstraints).execute_request(request_id)
        self.schedule_assessments()
        return result

    def schedule_assessments(self):
        groups = {
            r["assessment_group"]: r
            for r in self.w["reserve_routes"].values()
            if r.get("scope") == self.w["_session_scope"] and r.get("pending_assessment")
        }
        for group, r in groups.items():
            if any(
                e["kind"] == "desk_reserve_assessment" and e["target_id"] == group
                for e in self.w["events"].values()
            ):
                continue
            if self.reserve_ready(r["assessment_conditions"]):
                self.schedule("desk_reserve_assessment", group, r["assessment_delay"])

    def propose_reserve_disbursement(self, route_id):
        r = self.row("reserve_routes", route_id)
        now = self.w["minute"]
        self.check(
            r.get("available_from", 0) <= now <= r.get("expires_minute", 1000000),
            "RESERVE_BOOKING_WINDOW_CLOSED",
        )
        self.check(
            not any(d["route_id"] == route_id for d in self.w["reserve_disbursements"].values()),
            "RESERVE_OPTION_ALREADY_USED",
        )
        self.check(self.reserve_ready(r["conditions"]), "RESERVE_RELEASE_CONDITIONS_NOT_MET")
        self._reserve_release = True
        try:
            self.desk_debit(
                r["reserve_account_id"], "USD", r["debit_minor"], "reserve_disbursement", route_id
            )
        finally:
            self._reserve_release = False
        delay = r.get("settlement_delay", 0)
        d = self.new(
            "reserve_disbursements",
            route_id=route_id,
            reserve_account_id=r["reserve_account_id"],
            destination_account_id=r["destination_account_id"],
            debit_minor=r["debit_minor"],
            net_minor=r["net_minor"],
            fee_minor=r["debit_minor"] - r["net_minor"],
            minute=now,
            settles_minute=now + delay,
            terms_version=r.get("terms_version", 1),
            status="IN_TRANSIT" if delay else "SETTLED",
        )
        if delay:
            self.schedule("desk_reserve_settlement", d["id"], delay)
        else:
            self.desk_credit(
                r["destination_account_id"], "USD", r["net_minor"], "reserve_disbursement", route_id
            )
        return self.public(d)

    def apply_event(self, event):
        if event["kind"] == "desk_reserve_settlement":
            d = self.row("reserve_disbursements", event["target_id"])
            self.desk_credit(
                d["destination_account_id"],
                "USD",
                d["net_minor"],
                "reserve_disbursement",
                d["route_id"],
            )
            d["status"] = "SETTLED"
        elif event["kind"] == "desk_reserve_assessment":
            for r in self.w["reserve_routes"].values():
                if r.get("assessment_group") == event["target_id"]:
                    r.update(deepcopy(r["_assessed_terms"]))
                    r.update(
                        pending_assessment=False,
                        terms_version=r.get("terms_version", 1) + 1,
                        assessed_minute=self.w["minute"],
                    )
                    r["fee_minor"] = r["debit_minor"] - r["net_minor"]
        else:
            self.next_handler(CustomerConstraints).apply_event(event)
        self.schedule_assessments()
