"""Tau text orchestrator with uncapped, observable tool-use counts."""

from __future__ import annotations

from math import inf

from tau2.orchestrator.orchestrator import Orchestrator, Role


class CountingOrchestrator(Orchestrator):
    def __init__(self, *args, max_steps=None, max_tool_calls=None, **kwargs):
        super().__init__(*args, max_steps=inf, **kwargs)
        self.max_tool_calls = inf
        self.attempted_tool_calls = 0
        self.executed_tool_batches = 0
        self.budget_stop = None

    def step(self):
        if self.to_role == Role.ENV:
            self.attempted_tool_calls += len(self.message.tool_calls)
            self.executed_tool_batches += 1
        super().step()
