"""Operational backing shared by current instructions and standing commitments.
No task ID or evaluator target is visible to the transition functions.
"""

from copy import deepcopy
from payment.runtime.products.credit.engine import BusinessError
from payment.runtime.products.resources.specification import (
    CAPABILITIES,
    NEW_CAPABILITIES,
    RESOURCE_TABLES,
)


class ResourceOperations:

    def get_record(self, resource, record_id):
        if resource in RESOURCE_TABLES:
            return self.public(self.row(resource, record_id))
        return self.next_handler(ResourceOperations).get_record(resource, record_id)

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in RESOURCE_TABLES:
            return self.next_handler(ResourceOperations).search_records(
                resource, filters, cursor, limit
            )
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        self.check(all(not k.startswith("_") for k in (filters or {})), "FIELD_NOT_PUBLIC")
        rows = [
            self.public(r)
            for r in self.w[resource].values()
            if all(r.get(k) == v for k, v in (filters or {}).items())
        ]
        rows.sort(key=lambda r: r["id"])
        return dict(
            records=[
                {
                    k: r[k]
                    for k in ("id", "reference", "status", "scope", "account_id", "name")
                    if k in r
                }
                for r in rows[cursor : cursor + limit]
            ],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW_CAPABILITIES:
            return self.next_handler(ResourceOperations).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        spec = NEW_CAPABILITIES[capability_name]
        self.check(set(arguments) == set(spec["parameters"]), "INVALID_CAPABILITY_ARGUMENTS")
        for k, t in spec["parameters"].items():
            self.check(
                type(arguments[k]) is {"integer": int, "string": str, "object": dict}[t],
                "INVALID_CAPABILITY_ARGUMENT_TYPE",
            )
        return self.new(
            "requests",
            operation=capability_name,
            arguments=deepcopy(arguments),
            status="PENDING",
            prepared_minute=self.w["minute"],
        )

    def pool_used(self, pool_id):
        return sum(
            r["pool_debit"]
            for r in self.w["capacity_reservations"].values()
            if r["pool_id"] == pool_id and r["status"] in ("RESERVED", "CONSUMED")
        )

    def propose_capacity_reservation(self, pool_id, obligation_id, quantity):
        p = self.row("capacity_pools", pool_id)
        o = self.row("capacity_obligations", obligation_id)
        self.check(
            o["status"] == "OPEN" and pool_id in o["eligible_pools"] and p["scope"] == o["scope"],
            "CAPACITY_SCOPE_OR_STATUS",
        )
        self.check(quantity > 0 and quantity % p["coverage_lot"] == 0, "INVALID_COVERAGE_LOT")
        existing = sum(
            r["quantity"]
            for r in self.w["capacity_reservations"].values()
            if r["obligation_id"] == obligation_id and r["status"] == "RESERVED"
        )
        self.check(existing + quantity <= o["required_quantity"], "COVERAGE_ABOVE_INSTRUCTION")
        debit = quantity * o.get("pool_cost_multipliers", {}).get(pool_id, 1)
        self.check(self.pool_used(pool_id) + debit <= p["capacity"], "POOL_CAPACITY_NOT_AVAILABLE")
        return self.new(
            "capacity_reservations",
            pool_id=pool_id,
            obligation_id=obligation_id,
            quantity=quantity,
            pool_debit=debit,
            pool_version=p["version"],
            status="RESERVED",
            reserved_minute=self.w["minute"],
        )

    def propose_capacity_reservations(self, allocations):
        self.check(bool(allocations), "EMPTY_ALLOCATION")
        out = []
        for oid, parts in allocations.items():
            self.check(type(parts) is dict and bool(parts), "INVALID_ALLOCATION_MAP")
            for pid, quantity in parts.items():
                self.check(type(quantity) is int, "INVALID_QUANTITY")
                out.append(self.propose_capacity_reservation(pid, oid, quantity))
        return dict(reservations=out)

    def propose_capacity_release(self, reservation_id):
        r = self.row("capacity_reservations", reservation_id)
        self.check(r["status"] in ("RESERVED", "STALE"), "CAPACITY_NOT_RELEASABLE")
        r.update(status="RELEASED", released_minute=self.w["minute"])
        return self.public(r)

    def coverage(self, o):
        return [
            r
            for r in self.w["capacity_reservations"].values()
            if r["obligation_id"] == o["id"] and r["status"] == "RESERVED"
        ]

    def consume_coverage(self, o, origin):
        rows = self.coverage(o)
        self.check(
            sum(r["quantity"] for r in rows) == o["required_quantity"],
            "OPERATIONAL_COVERAGE_INCOMPLETE",
        )
        for r in rows:
            p = self.row("capacity_pools", r["pool_id"])
            self.check(
                r["pool_version"] == p["version"] and self.pool_used(p["id"]) <= p["capacity"],
                "COVERAGE_VERSION_OR_LIMIT",
            )
        for r in rows:
            r.update(status="CONSUMED", consumed_minute=self.w["minute"], origin=origin)
        o.update(status="FULFILLED", fulfilled_minute=self.w["minute"], origin=origin)

    def execute_request(self, request_id):
        req = self.row("requests", request_id)
        matches = []
        if req["status"] == "EXECUTED":
            return self.next_handler(ResourceOperations).execute_request(request_id)
        for o in self.w["capacity_obligations"].values():
            link = o.get("business_link", {})
            if link.get("operation") == req["operation"] and all(
                req["arguments"].get(k) == v for k, v in link.get("arguments", {}).items()
            ):
                self.check(o["status"] == "OPEN", "COVERED_INSTRUCTION_ALREADY_USED")
                matches.append(o)
            if (
                link.get("operation") == "portal_payment"
                and req["operation"] == "propose_portal_payment"
            ):
                q = self.row("quotes", req["arguments"]["quote_id"])
                if q.get("account_id") == link["account_id"]:
                    self.check(o["status"] == "OPEN", "COVERED_INSTRUCTION_ALREADY_USED")
                    matches.append(o)
        # Coverage and request execution are one atomic transition. Failed authorization
        # or a failed business precondition rolls back the coverage debit as well.
        for o in matches:
            self.consume_coverage(o, dict(kind="request", request_id=request_id))
        return self.next_handler(ResourceOperations).execute_request(request_id)

    def issue_attested_bank_letter(self, account_id, addressee):
        matches = [
            o
            for o in self.w["capacity_obligations"].values()
            if o.get("business_link", {}).get("operation") == "letter"
            and o["business_link"]["account_id"] == account_id
            and o["business_link"]["addressee"] == addressee
        ]
        for o in matches:
            self.check(o["status"] == "OPEN", "COVERED_INSTRUCTION_ALREADY_USED")
            self.consume_coverage(
                o, dict(kind="letter", account_id=account_id, addressee=addressee)
            )
        return self.next_handler(ResourceOperations).issue_attested_bank_letter(
            account_id, addressee
        )

    def apply_event(self, event):
        if event["kind"] == "capacity_review":
            p = self.row("capacity_pools", event["target_id"])
            p.update(
                capacity=self.w["_event_payloads"][event["id"]]["released_capacity"],
                version=p["version"] + 1,
                review_status="FINAL",
                reviewed_minute=self.w["minute"],
            )
            for r in self.w["capacity_reservations"].values():
                if r["pool_id"] == p["id"] and r["status"] == "RESERVED":
                    r["status"] = "STALE"
            return
        if event["kind"] == "capacity_standing_instruction":
            o = self.row("capacity_obligations", event["target_id"])
            try:
                self.consume_coverage(o, dict(kind="standing_event", event_id=event["id"]))
            except BusinessError:
                o.update(
                    status="FAILED",
                    failed_minute=self.w["minute"],
                    failure="Coverage was incomplete or unavailable at execution.",
                )
            return
        return self.next_handler(ResourceOperations).apply_event(event)
