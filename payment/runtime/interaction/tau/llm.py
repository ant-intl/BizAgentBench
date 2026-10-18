"""HTTP participants hosted by tau2's native text orchestrator.

No witness, contract, full world, or gold document list enters a normal agent
request. The user independently generates both text and tool calls.
"""

from __future__ import annotations

import json

from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import (
    AssistantMessage,
    UserMessage,
    ToolMessage,
    MultiToolMessage,
)
from tau2.user.user_simulator import UserSimulator



def wire_message(message):
    if isinstance(message, ToolMessage):
        return {"role": "tool", "tool_call_id": message.id, "content": message.content}
    result = {"role": message.role, "content": message.content}
    if getattr(message, "tool_calls", None):
        result["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in message.tool_calls
        ]
    return result


class HTTPAgent(LLMAgent):
    @property
    def system_prompt(self):
        return (
            "You are the customer-support agent. In each turn either speak naturally in English to the customer or call tools. Do not wrap customer-facing text in JSON.\n\n"
            + self.domain_policy
        )

    def __init__(self, tools, domain_policy, endpoint):
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=endpoint.config["model"], llm_args={}
        )
        self.endpoint = endpoint

    def set_seed(self, seed):
        self.endpoint.seed = seed

    def _generate_next_message(self, message, state):
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        elif message is not None:
            state.messages.append(message)
        return self.endpoint.complete(state.system_messages + state.messages, self.tools)


class HTTPUser(UserSimulator):
    def __init__(self, instructions, tools, endpoint):
        super().__init__(
            llm=endpoint.config["model"], instructions=instructions, tools=tools, llm_args={}
        )
        self.endpoint = endpoint

    def set_seed(self, seed):
        self.endpoint.seed = seed

    def _generate_next_message(self, message, state):
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        elif message is not None:
            state.messages.append(message)
        return self.endpoint.complete(state.system_messages + state.flip_roles(), self.tools)
