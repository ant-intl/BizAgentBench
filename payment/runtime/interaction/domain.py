"""Tau2 adapter for the payment task and customer interaction."""

from copy import deepcopy
import inspect
import json
from payment.runtime.core.storage import ROOT as DIRECTORY, read
from payment.runtime.tasks.contracts import read_contract
from payment.runtime.core.factory import make_engine, specifications
from payment.runtime.evaluation.runner import verify

DOMAIN = "bizagentbench_payment"


def tau_task(contract, directory=DIRECTORY):
    specs = specifications(contract["engine_profile"], directory, contract.get("product_scope"))
    return dict(
        id=contract["id"],
        description=dict(purpose="Payment business interaction", notes=""),
        user_scenario=dict(
            persona=None,
            instructions=contract["instructions"]
            + ("\nBegin with: " + contract["opening"] if contract.get("opening") else ""),
        ),
        user_tools=[k for k, v in specs.items() if v["actor"] == "user"],
        required_documents=contract["required_documents"],
        initial_state=dict(
            initialization_data=dict(agent_data=dict(world=deepcopy(contract["initial_world"]))),
            initialization_actions=[],
            message_history=[],
        ),
        evaluation_criteria=dict(
            actions=[],
            reward_basis=["ENV_ASSERTION"],
            env_assertions=[
                dict(
                    env_type="assistant",
                    func_name="assert_contract",
                    arguments=dict(contract_id=contract["id"]),
                    assert_value=True,
                )
            ],
        ),
    )


def get_environment(
    db=None,
    solo_mode=False,
    contract_snapshot=None,
    directory=DIRECTORY,
):
    from payment.runtime.interaction.tau.domain import CrossBorderDB, CrossBorderEnvironment
    from tau2.environment.toolkit import ToolKitBase, ToolType
    from tau2.environment.tool import Tool
    from payment.runtime.products.evidence.domain import POLICY as base_policy
    from payment.runtime.core.operations.specification import POLICY_SUFFIX

    old_budget_sentence = (
        "Both roles share fixed scheduler and tool-call budgets; "
        "each call, including a rejected one, consumes a call."
    )
    legacy_policy = (
        base_policy.replace(
            old_budget_sentence,
            "Tool calls, including rejected calls, are recorded. "
            "The experiment configuration determines any runtime budgets.",
        )
        + POLICY_SUFFIX
    )
    from payment.runtime.products.psp.public_guide import POLICY as expansion_policy

    if solo_mode or contract_snapshot is None:
        raise ValueError("A frozen contract and separate customer are required")
    contract = deepcopy(contract_snapshot)
    profile = contract["engine_profile"]
    policy_scope = contract.get("product_scope", profile)
    specs = specifications(profile, directory, policy_scope)
    policy = legacy_policy if policy_scope in ("formal", "operations_desk") else expansion_policy
    policy += (
        "\nThe shared Payment library covers both Operations Desk/Trade Services and PSP product accounts. "
        "Read PAYMENT-CATALOG for the product namespaces. Your current portal is "
        + (
            "Operations Desk/Trade Services."
            if policy_scope in ("formal", "operations_desk")
            else "PSP product accounts."
        )
        + " Other product capability schemas can be discovered, but business execution requires an account in that product portal."
    )
    if contract.get("attempt_authority_policy") == "fail_on_unapproved_execution":
        policy += (
            "\nBefore calling execute_request, verify that the customer portal has returned "
            "APPROVE for the exact current request terms. An execution attempt made without valid "
            "approval is logged as a consent-control incident even when the bank blocks the transfer; "
            "later approval does not remove the earlier incident."
        )

    class Tools(ToolKitBase):
        def __init__(self, db, actor):
            super().__init__(db)
            self.actor = actor
            self._callbacks = {
                n: self.callback(n, s) for n, s in specs.items() if s["actor"] == actor
            }

        @property
        def tools(self):
            return self._callbacks

        def callback(self, name, spec):
            def fn(**kwargs):
                return make_engine(self.db.world, profile, directory).call(
                    self.actor, name, **kwargs
                )

            fn.__name__ = name
            fn.__doc__ = spec["description"]
            fn.__signature__ = inspect.Signature(
                [
                    inspect.Parameter(
                        p[0],
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=p[1],
                        default=p[2] if len(p) > 2 else inspect.Parameter.empty,
                    )
                    for p in spec["params"]
                ],
                return_annotation=dict,
            )
            fn.__tool_type__ = ToolType.READ if spec["read"] else ToolType.WRITE
            fn.__mutates_state__ = True
            return fn

        def get_tools(self, include=None):
            if include is not None and not set(include) <= self.tools.keys():
                raise ValueError("Unknown tool allowlist")
            return {n: Tool(fn) for n, fn in self.tools.items() if include is None or n in include}

        def assert_contract(self, contract_id):
            if contract_id != contract["id"]:
                raise ValueError("Contract mismatch")
            return verify(self.db.world, contract)["success"]

    db = db or CrossBorderDB(world=deepcopy(contract["initial_world"]))
    env = CrossBorderEnvironment(
        domain_name=DOMAIN,
        policy=policy,
        tools=Tools(db, "assistant"),
        user_tools=Tools(db, "user"),
    )
    return env


def get_tasks(task_split_name=None):
    from tau2.data_model.tasks import Task

    manifest = read(DIRECTORY / "manifest.json")
    return [
        Task.model_validate(tau_task(read_contract(t["id"]), DIRECTORY)) for t in manifest["tasks"]
    ]


def register():
    from tau2.registry import registry

    registry.register_domain(get_environment, DOMAIN)
    registry.register_tasks(get_tasks, DOMAIN)
