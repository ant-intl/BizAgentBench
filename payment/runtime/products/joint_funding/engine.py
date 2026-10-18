"""Shared funding and settlement allocation effects."""

from copy import deepcopy

from payment.runtime.products.joint_funding.records import TABLES
from payment.runtime.products.joint_funding.specification import CAPABILITIES, NAME


class JointFunding:

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name != NAME:
            return self.next_handler(JointFunding).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        spec = CAPABILITIES[NAME]["parameters"]
        self.check(set(arguments) == set(spec), "INVALID_CAPABILITY_ARGUMENTS")
        types = {"string": str, "integer": int, "object": dict}
        for k, t in spec.items():
            self.check(type(arguments[k]) is types[t], "INVALID_CAPABILITY_ARGUMENT_TYPE")
        return self.public(
            self.new(
                "requests",
                operation=NAME,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in TABLES:
            return self.next_handler(JointFunding).search_records(resource, filters, cursor, limit)
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        self.check(not any(k.startswith("_") for k in (filters or {})), "FIELD_NOT_PUBLIC")
        rows = [
            self.public(r, resource)
            for r in self.w.get(resource, {}).values()
            if r["scope"] == self.w["_session_scope"]
        ]
        for k, v in (filters or {}).items():
            rows = [r for r in rows if r.get(k) == v]
        rows.sort(key=lambda r: r["id"])
        fields = ("id", "name", "reference", "case_id", "agreement_id", "status", "currency")
        return dict(
            records=[{k: r[k] for k in fields if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource not in TABLES:
            return self.next_handler(JointFunding).get_record(resource, record_id)
        r = self.row(resource, record_id)
        self.check(r["scope"] == self.w["_session_scope"], "ACCESS_DENIED")
        return self.public(r, resource)

    def propose_joint_capital_delivery(
        self, agreement_id, case_id, route_id, allocations, terms_version
    ):
        o = self.row("joint_agreements", agreement_id)
        r = self.row("joint_routes", route_id)
        self.check(
            o["scope"] == self.w["_session_scope"]
            and r["agreement_id"] == agreement_id
            and r["case_id"] == case_id,
            "WRONG_CAPITAL_ARRANGEMENT",
        )
        prior = [f for f in self.w["joint_fundings"].values() if f["case_id"] == case_id]
        delivered = sum(f["amount_minor"] for f in prior)
        fee = r["fee_minor"] if not prior else 0
        remaining = (
            o["capital_by_case"][case_id] - o["initial_operating_by_case"][case_id] - delivered
        )
        self.check(r["terms_version"] == terms_version, "ROUTE_TERMS_CHANGED")
        self.check(
            allocations
            and all(type(n) is int and n > 0 for n in allocations.values())
            and sum(allocations.values()) <= remaining,
            "CAPITAL_AMOUNT_MISMATCH",
        )
        charges = dict(allocations)
        charges[o["fee_source_id"]] = charges.get(o["fee_source_id"], 0) + fee
        for sid, n in charges.items():
            s = self.row("joint_sources", sid)
            self.check(
                s["agreement_id"] == agreement_id and s["balance_minor"] >= n,
                "ALLOCATION_BALANCE_INSUFFICIENT",
            )
            self.check(s.get("status") == "AVAILABLE", "ALLOCATION_NOT_RELEASED")
        # Customer commercial earmarks are not a bank transfer permission check.
        for sid, n in charges.items():
            self.w["joint_sources"][sid]["balance_minor"] -= n
        self.w["service_cases"][case_id]["cash_minor"] += sum(allocations.values())
        if r.get("destination_account_id"):
            self.w["service_records"][r["destination_account_id"]]["cash_minor"] += sum(
                allocations.values()
            )
        return self.public(
            self.new(
                "joint_fundings",
                agreement_id=agreement_id,
                case_id=case_id,
                route_id=route_id,
                allocations=deepcopy(allocations),
                amount_minor=sum(allocations.values()),
                fee_minor=fee,
                terms_version=terms_version,
                minute=self.w["minute"],
                status="SETTLED",
            )
        )
