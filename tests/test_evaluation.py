"""Offline regressions for error accounting, frozen assets, and G/P/F scoring."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from tools import evaluate
from payment.runtime.evaluation.process import dimensioned
from payment.runtime.interaction.domain import get_environment, register
from payment.runtime.interaction.transport.endpoint_base import ReasoningEndpoint
from payment.runtime.tasks.contracts import read_contract
from tau2.data_model.message import ToolCall


class MemoryClient:
    """Return scripted Chat Completions without making network requests."""

    def __init__(self, role, mode):
        self.role = role
        self.mode = mode
        self.count = 0

    def post(self, url, json, **kwargs):
        import httpx

        self.count += 1
        if self.mode == "network_error":
            raise httpx.ConnectError("Offline transport failure")
        if self.role == "user":
            message = {"content": "Please help with my payment." if self.count == 1 else "###STOP###"}
        elif self.mode == "business_errors" and self.count <= 18:
            message = {
                "tool_calls": [{
                    "id": f"call_{self.count}",
                    "type": "function",
                    "function": {
                        "name": "execute_request",
                        "arguments": '{"request_id":"NO_SUCH_REQUEST"}',
                    },
                }],
            }
        elif self.count == 1:
            message = {
                "tool_calls": [{
                    "id": "clock_call",
                    "type": "function",
                    "function": {"name": "get_clock", "arguments": "{}"},
                }],
            }
        else:
            message = {"content": "I cannot complete the request."}
        if self.mode == self.role + "_protocol_error":
            message = {
                "tool_calls": [{
                    "id": "invalid_call",
                    "type": "function",
                    "function": {"name": "get_clock", "arguments": "{bad-json"},
                }],
            }
        return httpx.Response(200, json={
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {},
        })

    def close(self):
        pass


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        register()

    def run_offline(self, mode, max_errors=16):
        temporary = tempfile.TemporaryDirectory(prefix="bizagentbench-test-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name)

        def endpoint(config, role, folder, deadline):
            result = ReasoningEndpoint(config, role, folder / "api_calls.jsonl")
            result.client.close()
            result.client = MemoryClient(role, mode)
            return result

        role_config = {"model": "offline", "base_url": "http://localhost/v1"}
        config = {
            "agent": deepcopy(role_config),
            "user": deepcopy(role_config),
            "max_errors": max_errors,
            "episode_timeout_seconds": 60,
            "seed": 0,
        }
        with patch("tools.evaluate.endpoint", side_effect=endpoint):
            result = evaluate.run_case("case_031", config, output)
        summary = evaluate.summarize(output, [{"id": "case_031", "number": 31}])
        return result, summary

    def test_business_refusal_counts_towards_error_threshold(self):
        result, summary = self.run_offline("business_errors", max_errors=1)
        self.assertEqual(result["actual_tool_calls"], 1)
        self.assertEqual(result["tool_errors"], 1)
        self.assertEqual(result["termination"].lower(), "too_many_errors")
        self.assertEqual(summary["evaluated"], 1)

    def test_successful_tool_call_does_not_consume_error_budget(self):
        result, summary = self.run_offline("normal", max_errors=1)
        self.assertEqual(result["actual_tool_calls"], 1)
        self.assertEqual(result["tool_errors"], 0)
        self.assertEqual(summary["evaluated"], 1)
        self.assertEqual(result["reward"], float(result["verification"]["strict_success"]))
        self.assertEqual(summary["strict_successes"], summary["paper_counts"]["strict_success"])

    def test_agent_protocol_error_is_scored_and_remains_in_denominator(self):
        result, summary = self.run_offline("assistant_protocol_error")
        self.assertEqual(result["classification"], "agent_protocol_error")
        self.assertEqual(result["reward"], 0)
        self.assertEqual(summary["evaluated"], 1)
        self.assertEqual(summary["paper_rates"]["strict_success"], 0)
        self.assertEqual(result["actual_tool_calls"], 0)

    def test_simulator_protocol_error_is_not_blame_assigned_to_agent(self):
        result, summary = self.run_offline("user_protocol_error")
        self.assertEqual(result["classification"], "simulator_protocol_error")
        self.assertIsNone(result["reward"])
        self.assertEqual(summary["evaluated"], 0)

    def test_network_error_remains_an_infrastructure_failure(self):
        result, summary = self.run_offline("network_error")
        self.assertEqual(result["classification"], "infrastructure_invalid")
        self.assertIsNone(result["reward"])
        self.assertEqual(summary["evaluated"], 0)

    def test_both_roles_receive_flagged_business_errors_with_original_receipts(self):
        contract = read_contract("case_031")
        environment = get_environment(contract_snapshot=contract)
        for role, name, arguments in (
            ("assistant", "execute_request", {"request_id": "NO_SUCH_REQUEST"}),
            ("user", "decide_request", {"request_id": "NO_SUCH_REQUEST", "decision": "APPROVE"}),
        ):
            with self.subTest(role=role):
                response = environment.get_response(ToolCall(
                    id=role + "_call", requestor=role, name=name, arguments=arguments,
                ))
                self.assertTrue(response.error)
                self.assertIs(json.loads(response.content)["ok"], False)
        self.assertEqual(len(environment.tools.db.world["audit"]), 2)


class AssetLockTests(unittest.TestCase):
    def test_real_user_prompt_is_part_of_asset_lock(self):
        from tau2.user.user_simulator import GLOBAL_USER_SIM_GUIDELINES_PATH_TOOLS

        key = str(GLOBAL_USER_SIM_GUIDELINES_PATH_TOOLS.relative_to(evaluate.ROOT))
        self.assertIn(key, evaluate.asset_lock())

    def test_prompt_change_changes_asset_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "third_party/tau2-bench/data/tau2/user_simulator/guidelines.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("First prompt.\n")
            with patch.object(evaluate, "ROOT", root), patch.object(evaluate, "DATA", root / "payment/data"):
                before = evaluate.asset_lock()
                prompt.write_text("Changed prompt.\n")
                after = evaluate.asset_lock()
            key = str(prompt.relative_to(root))
            self.assertNotEqual(before[key], after[key])


class PublishedScoringTests(unittest.TestCase):
    def test_gpf_is_the_only_aggregate_when_source_projection_changes_verdict(self):
        # The projected wallet check uses posted balances instead of the
        # source predicate's incomplete reconstruction of a linked workflow.
        wallet = {"USD": 10000, "EUR": 0, "GBP": 0}
        world = {
            "service_cases": {
                "fx": {"id": "fx", "kind": "contingent_forwards", "cash_minor": 10000,
                       "state": {"eur_minor": 0, "gbp_minor": 0}},
            },
            "service_bookings": {
                "posting": {"id": "posting", "case_id": "fx", "minute": 1,
                            "wallet_after_minor": wallet},
            },
        }
        contract = {"id": "case_010", "_dimension_registry": "bizagentbench-v1", "initial_world": {}}
        source = {"checks": [{"name": "activity1:ledger.currencies_and_margin", "passed": False}]}
        result = dimensioned(source, world, contract)
        self.assertTrue(result["success"])
        self.assertTrue(result["strict_success"])
        self.assertEqual(result["classification"], "success")
        self.assertEqual(result["failed"], [])
        self.assertTrue(all(row["passed"] for row in result["checks"]))
        self.assertFalse(result["source_checks"][0]["passed"])
        for field in ("goal_success", "process_success", "protection_success", "strict_success"):
            self.assertEqual(result[field], result["paper_metrics"][field])
        self.assertNotIn("outcome_success", result)
        self.assertNotIn("integrity_success", result)


if __name__ == "__main__":
    unittest.main()
