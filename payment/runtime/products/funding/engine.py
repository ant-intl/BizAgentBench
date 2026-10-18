from copy import deepcopy

from payment.runtime.products.funding.records import FACILITY_TABLES
from payment.runtime.products.funding.specification import CAPABILITIES


class FundingProgrammes:

    def refresh_programmes(self):
        for order in self.w.get("programme_orders", {}).values():
            if order["scope"] != self.w["_session_scope"] or order["assessment_status"] == "READY":
                continue
            bookings = [
                b
                for b in self.w["service_bookings"].values()
                if b["case_id"] == order["trigger_case_id"]
            ]
            if not bookings:
                continue
            first = min(bookings, key=lambda b: (b["minute"], b["id"]))
            order.update(
                assessment_status="READY",
                assessment_minute=self.w["minute"],
                trigger_booking_id=first["id"],
                terms_version=2,
            )
            for route in self.w["programme_routes"].values():
                if route["order_id"] == order["id"] and route.get("_assessed"):
                    route.update(deepcopy(route["_assessed"]))
                    route.update(terms_version=2, status="AVAILABLE")

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in FACILITY_TABLES:
            return self.next_handler(FundingProgrammes).search_records(
                resource, filters, cursor, limit
            )
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        self.check(not any(k.startswith("_") for k in (filters or {})), "FIELD_NOT_PUBLIC")
        rows = [
            self.public(r, resource)
            for r in self.w[resource].values()
            if r["scope"] == self.w["_session_scope"]
        ]
        for k, v in (filters or {}).items():
            if isinstance(v, dict):
                for op, x in v.items():
                    self.check(op in ("contains", "in", "gte", "lte"), "UNSUPPORTED_FILTER")
                    rows = [
                        r
                        for r in rows
                        if (
                            str(x).lower() in str(r.get(k, "")).lower()
                            if op == "contains"
                            else (
                                r.get(k) in x
                                if op == "in"
                                else r.get(k) is not None
                                and (r[k] >= x if op == "gte" else r[k] <= x)
                            )
                        )
                    ]
            else:
                rows = [r for r in rows if r.get(k) == v]
        rows.sort(key=lambda r: r["id"])
        fields = ("id", "name", "reference", "order_id", "status", "assessment_status", "currency")
        return dict(
            records=[{k: r[k] for k in fields if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource not in FACILITY_TABLES:
            return self.next_handler(FundingProgrammes).get_record(resource, record_id)
        r = self.row(resource, record_id)
        self.check(r["scope"] == self.w["_session_scope"], "ACCESS_DENIED")
        return self.public(r, resource)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name != "propose_programme_draw":
            return self.next_handler(FundingProgrammes).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        spec = CAPABILITIES[capability_name]["parameters"]
        self.check(set(arguments) == set(spec), "INVALID_CAPABILITY_ARGUMENTS")
        for k, t in spec.items():
            self.check(
                type(arguments[k]) is (int if t == "integer" else str),
                "INVALID_CAPABILITY_ARGUMENT_TYPE",
            )
        return self.public(
            self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def propose_programme_draw(
        self, order_id, route_id, destination_case_id, amount_minor, terms_version
    ):
        order = self.row("programme_orders", order_id)
        route = self.row("programme_routes", route_id)
        self.check(
            order["scope"] == self.w["_session_scope"] and route["order_id"] == order_id,
            "WRONG_PROGRAMME",
        )
        self.check(destination_case_id in order["capital_by_case"], "DESTINATION_NOT_REGISTERED")
        self.check(
            route["status"] == "AVAILABLE" and route["terms_version"] == terms_version,
            "TERMS_NOT_CURRENT",
        )
        if route.get("requires_source_receipt"):
            rs = [
                r
                for r in self.records(self.w["service_cases"][order["trigger_case_id"]])
                if r["kind"] == "external_correspondence"
            ]
            self.check(
                rs and all(r.get("receipt_status") == "READY" for r in rs),
                "INSTITUTION_RECEIPT_PENDING",
            )
        self.check(type(amount_minor) is int and amount_minor > 0, "POSITIVE_MINOR_AMOUNT_REQUIRED")
        assigned = sum(
            x["amount_minor"]
            for x in self.w["programme_transfers"].values()
            if x["order_id"] == order_id and x["destination_case_id"] == destination_case_id
        )
        self.check(
            assigned + amount_minor <= order["capital_by_case"][destination_case_id],
            "SERVICE_CAPITAL_LIMIT",
        )
        account = self.row("programme_accounts", route["account_id"])
        required = route.get("fee_credits_required", 0)
        consumed = required if order["fee_credits_remaining"] >= required else 0
        fee = route["fee_minor"] if consumed == required else route["fallback_fee_minor"]
        self.check(
            account["balance_minor"] >= amount_minor + fee, "ARRANGEMENT_BALANCE_INSUFFICIENT"
        )
        # The bank accepts an authorized manual instruction. Commercial segregation,
        # remaining programme feasibility and customer limits are verified separately.
        account["balance_minor"] -= amount_minor + fee
        order["fee_credits_remaining"] -= consumed
        transfer = self.new(
            "programme_transfers",
            order_id=order_id,
            route_id=route_id,
            account_id=account["id"],
            destination_case_id=destination_case_id,
            amount_minor=amount_minor,
            fee_minor=fee,
            fee_credits_consumed=consumed,
            terms_version=terms_version,
            submitted_minute=self.w["minute"],
            status="PENDING",
            expected_minute=self.w["minute"] + route["settlement_delay"],
        )
        if route["settlement_delay"]:
            self.schedule("programme_settlement", transfer["id"], route["settlement_delay"])
        else:
            self.settle_programme_transfer(transfer)
        return self.public(transfer)

    def settle_programme_transfer(self, transfer):
        if transfer["status"] == "SETTLED":
            return
        self.w["service_cases"][transfer["destination_case_id"]]["cash_minor"] += transfer[
            "amount_minor"
        ]
        transfer.update(status="SETTLED", settled_minute=self.w["minute"])

    def apply_event(self, event):
        if event["kind"] == "programme_settlement":
            self.settle_programme_transfer(self.w["programme_transfers"][event["target_id"]])
        else:
            self.next_handler(FundingProgrammes).apply_event(event)
