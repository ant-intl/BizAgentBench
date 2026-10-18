"""Unified public execution protocol over all Payment business handlers.

The old products contain hundreds of tested business transition methods.  They
are used here as handler libraries only.  This class owns the public call
boundary, argument validation, rollback, audit, document discovery and product
scope enforcement for every task.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, get_args, get_origin

from payment.runtime.knowledge.search import search
from payment.runtime.products.banking.engine import BusinessError as FormalBusinessError
from payment.runtime.products.psp.kernel import BusinessError as ExpansionBusinessError

from payment.runtime.core.execution.adapters import make_adapter
from payment.runtime.core.execution.specification import SPECS


class UnifiedBusinessError(Exception):
    pass


class Engine:
    def __init__(self, world, capabilities, documents, search_index, operations, event_handlers):
        self.w = world
        self.capabilities = capabilities
        self.documents = documents
        self.search_index = search_index
        self.operations = operations
        self.event_handlers = event_handlers
        self.w.setdefault("audit", [])
        self.w.setdefault("unlocked", [])
        self.w.setdefault("minute", 0)
        self.adapter = make_adapter(world, operations, documents, event_handlers)

    @property
    def product_scope(self):
        return self.adapter.product_scope

    def _validate_public_arguments(self, actor, name, arguments):
        spec = SPECS.get(name)
        if not spec or spec["actor"] != actor:
            raise UnifiedBusinessError("TOOL_NOT_AVAILABLE")
        names = {row[0] for row in spec["params"]}
        required = {row[0] for row in spec["params"] if len(row) == 2}
        if not required <= set(arguments) <= names:
            raise UnifiedBusinessError("INVALID_ARGUMENTS")
        for row in spec["params"]:
            if row[0] not in arguments:
                arguments[row[0]] = deepcopy(row[2])
            expected = row[1]
            value = arguments[row[0]]
            if get_origin(expected) is Literal:
                if value not in get_args(expected):
                    raise UnifiedBusinessError("INVALID_ARGUMENT_VALUE")
            elif type(value) is not expected:
                raise UnifiedBusinessError("INVALID_ARGUMENT_TYPE")

    def _closed(self):
        return self.adapter.is_closed()

    def _before_public_call(self):
        self.adapter.before_call()

    def _after_public_call(self, name):
        self.adapter.after_call(name)

    def call(self, actor, name, **arguments):
        """The only public call/rollback/audit boundary for the payment cases."""
        self._before_public_call()
        before = deepcopy(self.w)
        args = deepcopy(arguments)
        try:
            self._validate_public_arguments(actor, name, args)
            if self._closed() and not SPECS[name]["read"]:
                raise ExpansionBusinessError("CASE_ALREADY_FINALIZED")
            data = getattr(self, name)(**args)
            result = {"ok": True, "data": deepcopy(data)}
        except (FormalBusinessError, ExpansionBusinessError, UnifiedBusinessError) as exc:
            self.w.clear()
            self.w.update(before)
            result = {"ok": False, "error": str(exc)}
        except (KeyError, ValueError, TypeError) as exc:
            self.w.clear()
            self.w.update(before)
            # Preserve the historical public contract: implementation errors
            # are not exposed as Python details to a benchmark participant.
            result = {"ok": False, "error": "INVALID_ARGUMENTS"}
        self.w["audit"].append(
            dict(
                seq=len(self.w["audit"]) + 1,
                minute=self.w["minute"],
                actor=actor,
                tool=name,
                arguments=deepcopy(args),
                result=deepcopy(result),
            )
        )
        self._after_public_call(name)
        return deepcopy(result)

    def search_knowledge(self, query):
        return search(query, index_override=self.search_index)

    def read_document(self, document_id):
        if document_id not in self.documents:
            raise ExpansionBusinessError("DOCUMENT_NOT_FOUND")
        return deepcopy(self.documents[document_id])

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        return self.adapter.search_records(resource, filters, cursor, limit)

    def get_record(self, resource, record_id):
        return self.adapter.get_record(resource, record_id)

    def get_clock(self):
        return self.adapter.get_clock()

    def unlock_discoverable_agent_tool(self, capability_name):
        entry = self.capabilities.get(capability_name)
        if not entry:
            raise ExpansionBusinessError("UNKNOWN_TOOL")
        if capability_name not in self.w["unlocked"]:
            self.w["unlocked"].append(capability_name)
        return deepcopy(entry["definition"])

    def call_discoverable_agent_tool(self, capability_name, arguments):
        entry = self.capabilities.get(capability_name)
        if not entry:
            raise ExpansionBusinessError("UNKNOWN_TOOL")
        if entry["product_scope"] != self.product_scope:
            raise ExpansionBusinessError("PRODUCT_NOT_ENABLED_FOR_CURRENT_PORTAL")
        return self.adapter.call_capability(capability_name, arguments)

    def execute_request(self, request_id):
        return self.adapter.execute_request(request_id)

    def list_my_files(self):
        return self.adapter.list_my_files()

    def upload_file(self, file_id, target_id, purpose):
        return self.adapter.upload_file(file_id, target_id, purpose)

    def get_my_requests(self):
        return self.adapter.get_my_requests()

    def decide_request(self, request_id, decision):
        return self.adapter.decide_request(request_id, decision)

    def wait_for_update(self):
        return self.adapter.wait_for_update()

    def record_case_outcome(self, outcome, summary):
        return self.adapter.record_case_outcome(outcome, summary)

    # Customer-only actions used by Operations Desk tasks.  They remain normal
    # business handlers behind the same public call boundary.
    def accept_operator_invitation(self, operator_id):
        return self.adapter.invoke_customer_action(
            "accept_operator_invitation", operator_id=operator_id
        )

    def switch_operator(self, operator_id):
        return self.adapter.invoke_customer_action("switch_operator", operator_id=operator_id)

    def authenticate_payment(self, authentication_id):
        return self.adapter.invoke_customer_action(
            "authenticate_payment", authentication_id=authentication_id
        )

    def complete_portal_verification(self, challenge_id):
        return self.adapter.invoke_customer_action(
            "complete_portal_verification", challenge_id=challenge_id
        )

    def respond_group_invitation(self, member_id, decision):
        return self.adapter.invoke_customer_action(
            "respond_group_invitation", member_id=member_id, decision=decision
        )
