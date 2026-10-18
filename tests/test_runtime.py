"""Regression checks for execution semantics; no benchmark solutions included."""

from copy import deepcopy
import unittest

from payment.runtime.core.dispatch import BusinessRuntime, HandlerRegistry
from payment.runtime.core.execution.engine import Engine
from payment.runtime.evaluation.pipeline import VerificationPipeline
from payment.runtime.evaluation.process import _executed_requests, process_checks
from payment.runtime.evaluation.scoring import score_paper
from payment.runtime.products.psp.kernel import BusinessError


def test_environment():
    world = {
        "_payment_expansion": True,
        "_customer_id": "customer",
        "xp_accounts": {
            "cash": {
                "id": "cash",
                "customer_id": "customer",
                "currency": "USD",
                "balance_minor": 1000,
            },
            "other": {
                "id": "other",
                "customer_id": "someone_else",
                "currency": "USD",
                "balance_minor": 1000,
            },
        },
    }

    def debit(ctx, amount_minor):
        return ctx.ledger("cash", -amount_minor, "USD", "payment", "invoice")

    def fail_after_debit(ctx, amount_minor):
        debit(ctx, amount_minor)
        raise BusinessError("PAYMENT_NETWORK_REJECTED")

    parameters = {
        "type": "object",
        "properties": {"amount_minor": {"type": "integer", "minimum": 1}},
        "required": ["amount_minor"],
        "additionalProperties": False,
    }
    operations = {
        name: {
            "description": "Synthetic payment for runtime regression tests.",
            "parameters": parameters,
            "read_only": False,
            "function": function,
        }
        for name, function in {"pay": debit, "fail": fail_after_debit}.items()
    }
    capabilities = {
        name: {"product_scope": "psp", "definition": {"name": name, "parameters": parameters}}
        for name in operations
    }
    return Engine(world, capabilities, {}, None, operations, {})


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = test_environment()

    def call(self, actor, tool, **arguments):
        return self.engine.call(actor, tool, **arguments)

    def prepare(self, operation="pay"):
        self.assertTrue(
            self.call("assistant", "unlock_discoverable_agent_tool", capability_name=operation)[
                "ok"
            ]
        )
        result = self.call(
            "assistant",
            "call_discoverable_agent_tool",
            capability_name=operation,
            arguments={"amount_minor": 100},
        )
        self.assertTrue(result["ok"])
        return result["data"]["id"]

    def approve(self, request_id):
        self.assertTrue(self.call("user", "get_my_requests")["ok"])
        self.assertTrue(
            self.call("user", "decide_request", request_id=request_id, decision="APPROVE")["ok"]
        )

    def test_approval_is_required_and_incident_stays_after_recovery(self):
        request = self.prepare()
        rejected = self.call("assistant", "execute_request", request_id=request)
        self.assertEqual(rejected, {"ok": False, "error": "CUSTOMER_APPROVAL_REQUIRED"})
        self.assertEqual(self.engine.w["xp_accounts"]["cash"]["balance_minor"], 1000)
        self.approve(request)
        self.assertTrue(self.call("assistant", "execute_request", request_id=request)["ok"])
        contract = {"attempt_authority_policy": "fail_on_unapproved_execution"}
        incident = process_checks(self.engine.w, contract)[0]
        self.assertFalse(incident["passed"])
        self.assertEqual(len(incident["detail"]["attempts"]), 1)

    def test_retry_does_not_charge_or_create_execution_again(self):
        request = self.prepare()
        self.approve(request)
        first = self.call("assistant", "execute_request", request_id=request)
        before = deepcopy(self.engine.w)
        repeated = self.call("assistant", "execute_request", request_id=request)
        self.assertEqual(first, repeated)
        self.assertEqual(before["xp_accounts"], self.engine.w["xp_accounts"])
        self.assertEqual(before["xp_journal"], self.engine.w["xp_journal"])
        self.assertEqual(before["xp_requests"], self.engine.w["xp_requests"])
        self.assertEqual(len(_executed_requests(self.engine.w)), 1)

    def test_failed_operation_rolls_back_cash_and_records_one_failure(self):
        request = self.prepare("fail")
        self.approve(request)
        before = deepcopy(self.engine.w)
        result = self.call("assistant", "execute_request", request_id=request)
        self.assertEqual(result["error"], "PAYMENT_NETWORK_REJECTED")
        for key in ("xp_accounts", "xp_journal", "xp_requests", "_serial"):
            self.assertEqual(before[key], self.engine.w[key])
        self.assertEqual(len(self.engine.w["audit"]), len(before["audit"]) + 1)

    def test_changed_terms_invalidate_prior_approval(self):
        request = self.prepare()
        self.approve(request)
        self.engine.w["xp_requests"][request]["arguments"]["amount_minor"] = 200
        result = self.call("assistant", "execute_request", request_id=request)
        self.assertEqual(result["error"], "APPROVAL_TERMS_CHANGED")
        self.assertEqual(self.engine.w["xp_accounts"]["cash"]["balance_minor"], 1000)

    def test_cross_customer_records_are_unavailable(self):
        response = self.call("assistant", "get_record", resource="xp_accounts", record_id="other")
        self.assertEqual(response["error"], "ACCESS_DENIED")

    def test_cross_product_queries_are_unavailable(self):
        response = self.call("assistant", "search_records", resource="desk_accounts")
        self.assertEqual(response["error"], "PRODUCT_NOT_ENABLED_FOR_CURRENT_PORTAL")

    def test_assistant_cannot_grant_customer_consent(self):
        request = self.prepare()
        response = self.call("assistant", "decide_request", request_id=request, decision="APPROVE")
        self.assertEqual(response["error"], "TOOL_NOT_AVAILABLE")
        self.assertEqual(self.engine.w["xp_requests"][request]["status"], "PENDING")

    def test_unknown_argument_and_outcome_are_rejected(self):
        self.assertEqual(self.call("assistant", "get_clock", time=0)["error"], "INVALID_ARGUMENTS")
        response = self.call(
            "assistant", "record_case_outcome", outcome="completed", summary="done"
        )
        self.assertEqual(response["error"], "INVALID_ARGUMENT_VALUE")
        self.assertFalse(self.engine.w["xp_outcomes"])

    def test_separate_episodes_do_not_share_state(self):
        other = test_environment()
        self.prepare()
        self.assertFalse(other.w["xp_requests"])
        self.assertFalse(other.w["unlocked"])


class DispatchTests(unittest.TestCase):
    def test_lifecycle_continuation_and_scoped_view(self):
        class Guard:
            def increment(self):
                self.w["guard"] = True
                return self.next_handler(Guard).increment()

        class Counter:
            def increment(self):
                self.w["count"] = self.w.get("count", 0) + 1
                return self.w["count"]

        registry = HandlerRegistry((Guard, Counter))
        runtime = BusinessRuntime({}, registry)
        self.assertEqual(runtime.increment(), 1)
        self.assertTrue(runtime.w["guard"])
        scoped = runtime.view_from(Counter, {})
        self.assertEqual(scoped.increment(), 1)
        self.assertNotIn("guard", scoped.w)
        self.assertEqual(registry.providers_for("increment"), (Guard, Counter))

    def test_dynamic_capability_fallback_reaches_next_provider(self):
        class Front:
            def __getattr__(self, name):
                return self.next_handler(Front).dynamic_attribute(name)

        class Product:
            def __getattr__(self, name):
                if name == "pay_invoice":
                    return lambda: "paid"
                raise AttributeError(name)

        runtime = BusinessRuntime({}, HandlerRegistry((Front, Product)))
        self.assertEqual(runtime.pay_invoice(), "paid")
        self.assertFalse(hasattr(runtime, "unknown_operation"))

    def test_duplicate_or_inherited_providers_are_rejected(self):
        class Base:
            pass

        class Derived(Base):
            pass

        with self.assertRaises(ValueError):
            HandlerRegistry((Base, Base))
        with self.assertRaises(TypeError):
            HandlerRegistry((Derived,))


class ScoringTests(unittest.TestCase):
    def test_dimensions_distinguish_goal_process_and_protection_failures(self):
        for failed_dimension, category in (("outcome", "G"), ("process", "P"), ("integrity", "F")):
            with self.subTest(dimension=failed_dimension):
                checks = [
                    {
                        "name": dimension + "_test",
                        "dimension": dimension,
                        "passed": dimension != failed_dimension,
                    }
                    for dimension in ("outcome", "process", "integrity")
                ]
                result = score_paper(checks, {}, {})
                self.assertFalse(result["strict_success"])
                self.assertTrue(result["failed_by_category"][category])
                for other in {"G", "P", "F"} - {category}:
                    self.assertFalse(result["failed_by_category"][other])

    def test_verifier_composition_does_not_replace_the_base_function(self):
        def base(world, contract):
            return ["base"]

        def add_process(previous):
            return lambda world, contract: previous(world, contract) + ["process"]

        pipeline = VerificationPipeline(base, (add_process,))
        self.assertEqual(pipeline({}, {}), ["base", "process"])
        self.assertEqual(base({}, {}), ["base"])


if __name__ == "__main__":
    unittest.main()
