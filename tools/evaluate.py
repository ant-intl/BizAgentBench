"""Run the frozen fifty-case payment benchmark with two model endpoints."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "payment" / "data"
sys.path[:0] = [
    str(ROOT),
    str(ROOT / "third_party" / "tau2-bench" / "src"),
]

from tools.transport_support import read, write, digest
from payment.runtime.interaction.user_policy import USER_POLICY
from tools.payment_endpoint import endpoint


def asset_lock() -> dict[str, str]:
    tau_root = ROOT / "third_party" / "tau2-bench"
    paths = [
        path
        for folder in (
            DATA,
            ROOT / "payment" / "runtime",
            ROOT / "tools",
            tau_root / "src",
            tau_root / "data",
        )
        for path in folder.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".md", ".txt"}
    ]
    paths.extend(
        path for path in (ROOT / "requirements.txt", tau_root / "pyproject.toml")
        if path.is_file()
    )
    return {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in sorted(paths)}


def run_case(task_id: str, config: dict, output: Path) -> dict:
    from payment.runtime.interaction.tau.llm import HTTPAgent, HTTPUser
    from payment.runtime.interaction.domain import DOMAIN, get_environment, tau_task
    from payment.runtime.tasks.contracts import read_contract
    from payment.runtime.evaluation.runner import verify
    from tools.orchestrator import CountingOrchestrator
    from payment.runtime.interaction.transport.endpoint_base import ModelProtocolError
    from payment.runtime.interaction.transport.deadline import EpisodeTimeoutError
    from tau2.data_model.tasks import Task
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.runner.simulation import run_simulation

    folder = output / f"{task_id}__trial0"
    folder.mkdir(parents=True, exist_ok=True)
    contract = read_contract(task_id, DATA)
    task = Task.model_validate(tau_task(contract, DATA))
    started = time.monotonic()
    deadline = started + config["episode_timeout_seconds"]
    environment = get_environment(contract_snapshot=contract, directory=DATA)
    agent_endpoint = endpoint(config["agent"], "assistant", folder, deadline)
    user_endpoint = endpoint(config["user"], "user", folder, deadline)
    agent = HTTPAgent(environment.get_tools(), environment.get_policy(), agent_endpoint)
    user = HTTPUser(
        str(task.user_scenario) + "\n\n" + USER_POLICY, environment.get_user_tools(), user_endpoint
    )

    class CheckpointOrchestrator(CountingOrchestrator):
        def _check_timeout(self):
            from tau2.data_model.simulation import TerminationReason

            if not self.done and time.monotonic() >= deadline:
                self.done = True
                self.termination_reason = TerminationReason.TIMEOUT

        def step(self):
            # Use the transport's absolute deadline, including setup time.
            # Do not execute a queued tool call after the budget has expired.
            self._check_timeout()
            if self.done:
                return
            super().step()
            write(
                folder / "trajectory_live.json",
                [item.model_dump(mode="json") for item in self.get_trajectory()],
            )
            write(folder / "world_checkpoint.json", self.environment.tools.db.world)
            write(
                folder / "progress.json",
                {
                    "scheduler_steps": self.step_count,
                    "tool_calls": self.attempted_tool_calls,
                    "api_calls": len(agent_endpoint.calls) + len(user_endpoint.calls),
                    "elapsed_seconds": time.monotonic() - started,
                    "updated_at": time.time(),
                },
            )

        def _finalize(self):
            from tau2.orchestrator.orchestrator import Role

            if self.to_role == Role.ENV and not self.budget_stop:
                pending = self.message
                write(folder / "unexecuted_message.json", pending.model_dump(mode="json"))
                assert self.trajectory[-1] is pending
                self.trajectory.pop()
                self.to_role = self.from_role
                self.message = self.trajectory[-1]
            return super()._finalize()

    orchestrator = CheckpointOrchestrator(
        domain=DOMAIN,
        agent=agent,
        user=user,
        environment=environment,
        task=task,
        max_steps=None,
        max_tool_calls=None,
        max_errors=config["max_errors"],
        seed=config["seed"],
        timeout=config["episode_timeout_seconds"],
        validate_communication=False,
    )
    result = {
        "task_id": task_id,
        "number": contract["number"],
        "trial": 0,
        "agent_model": config["agent"]["model"],
        "user_model": config["user"]["model"],
    }
    try:
        simulation = run_simulation(
            orchestrator,
            evaluation_type=EvaluationType.ENV,
            env_kwargs={"contract_snapshot": contract, "directory": DATA},
        )
        write(folder / "trajectory.json", simulation.model_dump(mode="json"))
        result.update(
            reward=simulation.reward_info.reward, termination=simulation.termination_reason.value
        )
    except Exception as exc:
        result.update(
            reward=None,
            termination="RUN_ERROR",
            error_class=type(exc).__name__,
            error=str(exc)[:600],
        )
        if isinstance(exc, ModelProtocolError):
            result["protocol_error_role"] = exc.role
        elif isinstance(exc, EpisodeTimeoutError):
            result["termination"] = "timeout"
            result["timeout_role"] = exc.role
        write(
            folder / "trajectory_partial.json",
            [item.model_dump(mode="json") for item in orchestrator.get_trajectory()],
        )
    finally:
        world = environment.tools.db.world
        try:
            verdict = verify(world, contract)
        except Exception as exc:
            verdict = {
                "strict_success": False,
                "valid_run": False,
                "classification": "verifier_error",
                "error": str(exc)[:600],
            }
        result["verification"] = verdict
        protocol_role = result.get("protocol_error_role")
        if (
            protocol_role == "assistant" or result["termination"].lower() == "timeout"
        ) and verdict.get("valid_run") is not False:
            # Agent protocol failures and episode timeouts are evaluated from
            # the state reached, and remain in the success-rate denominator.
            result["reward"] = float(verdict["strict_success"])
        write(folder / "final_world.json", world)
        calls = agent_endpoint.calls + user_endpoint.calls
        audit = world.get("audit", [])
        result.update(
            actual_scheduler_steps=orchestrator.step_count,
            actual_tool_batches=orchestrator.executed_tool_batches,
            actual_tool_calls=orchestrator.attempted_tool_calls,
            tool_calls_by_name=dict(Counter(item["tool"] for item in audit)),
            database_queries=sum(
                item["tool"] in ("search_records", "get_record") for item in audit
            ),
            tool_errors=orchestrator.num_errors,
            api_calls=len(calls),
            usage={
                field: sum((item.get("usage") or {}).get(field, 0) or 0 for item in calls)
                for field in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
            duration_seconds=time.monotonic() - started,
        )
        limits = [
            item["role"]
            for item in calls
            if any(
                choice.get("finish_reason") == "length"
                for choice in (item.get("response") or {}).get("choices", [])
            )
        ]
        result["output_limit_roles"] = limits
        if verdict.get("valid_run") is False:
            result["classification"] = "verifier_error"
        elif protocol_role is not None:
            result["classification"] = (
                "agent_protocol_error" if protocol_role == "assistant"
                else "simulator_protocol_error"
            )
        elif result["termination"].lower() == "timeout":
            result["classification"] = "wall_clock_limit"
        elif result.get("reward") is not None:
            result["classification"] = verdict.get(
                "classification", "success" if verdict.get("strict_success") else "failure"
            )
            if not verdict.get("strict_success"):
                if "user" in limits:
                    result["classification"] = "simulator_output_limit"
                elif "assistant" in limits:
                    result["classification"] = "agent_output_limit"
        else:
            result["classification"] = "infrastructure_invalid"
        if (
            result.get("reward") is not None
            and verdict.get("valid_run") is not False
            and bool(result["reward"]) != bool(verdict["strict_success"])
        ):
            result["classification"] = "evaluator_replay_mismatch"
        agent_endpoint.close()
        user_endpoint.close()
        write(folder / "result.json", result)
    return result


def summarize(output: Path, tasks: list[dict]) -> dict:
    rows = sorted(
        [read(path) for path in output.glob("*__trial0/result.json")],
        key=lambda item: item["number"],
    )
    scored = [
        item
        for item in rows
        if item.get("reward") is not None
        and item.get("verification", {}).get("valid_run") is not False
    ]
    paper_fields = (
        "goal_success",
        "process_success",
        "protection_success",
        "process_protection_success",
        "strict_success",
    )
    paper_counts = {
        field: sum(
            bool(item["verification"].get(field)) for item in scored
        )
        for field in paper_fields
    }
    summary = {
        "planned": len(tasks),
        "finished": len(rows),
        "evaluated": len(scored),
        "strict_successes": paper_counts["strict_success"],
        "paper_counts": paper_counts,
        "paper_rates": {
            field: (count / len(scored) if scored else None)
            for field, count in paper_counts.items()
        },
        "classifications": dict(Counter(item["classification"] for item in rows)),
        "results": rows,
        "updated_at": time.time(),
    }
    write(output / "summary.json", summary)
    return summary


def main() -> None:
    from payment.runtime.interaction.domain import register

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--numbers", type=int, nargs="+")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.workers <= 50):
        parser.error("--workers must be between 1 and 50")
    config = read(args.config)
    for role in ("agent", "user"):
        role_config = config[role]
        if "api_key" in role_config:
            parser.error("Use api_key_env; do not place a key in the config file")
        if not os.getenv(role_config["api_key_env"]):
            parser.error(f"Missing environment variable: {role_config['api_key_env']}")
        if not role_config["base_url"].startswith(
            ("https://", "http://127.0.0.1:", "http://localhost:")
        ):
            parser.error(f"Unsupported URL scheme for {role}")
        role_config.setdefault("trust_env", False)
        role_config["compact_api_ledger"] = True
    config["simulator_prompt_overlay"] = USER_POLICY
    manifest = read(DATA / "manifest.json")
    tasks = [
        task for task in manifest["tasks"] if not args.numbers or task["number"] in args.numbers
    ]
    if not tasks:
        parser.error("No matching task numbers")
    output = args.output.resolve()
    lock = {"config": config, "assets": asset_lock(), "selected": tasks}
    if output.exists():
        if not args.resume or read(output / "experiment.json") != lock:
            parser.error("An existing run requires --resume with the same configuration and assets")
    else:
        output.mkdir(parents=True)
        write(output / "experiment.json", lock)
    register()
    pending = [
        task["id"]
        for task in tasks
        if not (output / f"{task['id']}__trial0" / "result.json").exists()
    ]
    summarize(output, tasks)
    print(f"START {len(pending)} cases with {args.workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_case, task_id, deepcopy(config), output): task_id for task_id in pending
        }
        for future in as_completed(futures):
            result = future.result()
            summary = summarize(output, tasks)
            print(
                json.dumps(
                    {
                        "number": result["number"],
                        "classification": result["classification"],
                        "strict_success": result["verification"].get("strict_success"),
                        "finished": summary["finished"],
                    }
                ),
                flush=True,
            )
    if asset_lock() != lock["assets"]:
        raise RuntimeError("Dataset or runtime changed during evaluation")
    write(
        output / "process.json",
        {
            "status": "completed",
            "finished": len(tasks),
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )


if __name__ == "__main__":
    main()
