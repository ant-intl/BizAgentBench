"""Deterministic settlement operations."""

from copy import deepcopy
from functools import partial

from payment.runtime.products.settlement.mechanisms import ENTRIES, BY_KIND, BY_TOOL, BY_EVENT
from payment.runtime.products.settlement.specification import CAPABILITIES, NEW


class SettlementServices:
    def __getattr__(self, name):
        if name in BY_TOOL:
            return partial(self.service_instruction, BY_TOOL[name].kind)
        return self.next_handler(SettlementServices).dynamic_attribute(name)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW:
            return self.next_handler(SettlementServices).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        self.check(
            set(arguments) == {"case_id", "action", "data"}
            and type(arguments["case_id"]) is str
            and type(arguments["action"]) is str
            and type(arguments["data"]) is dict,
            "INVALID_CAPABILITY_ARGUMENTS",
        )
        self.row("service_cases", arguments["case_id"])
        return self.public(
            self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def service_instruction(self, kind, case_id, action, data):
        if kind in BY_KIND:
            self.check(
                self.row("service_cases", case_id)["kind"] == kind, "SERVICE_CONTRACT_MISMATCH"
            )
            return getattr(self, BY_KIND[kind].operation_method)(case_id, action, data)
        return self.next_handler(SettlementServices).service_instruction(
            kind, case_id, action, data
        )

    def apply_event(self, event):
        if event["kind"] in BY_EVENT:
            return getattr(self, BY_EVENT[event["kind"]].event_method)(event)
        return self.next_handler(SettlementServices).apply_event(event)
