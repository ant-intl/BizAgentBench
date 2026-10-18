"""Preserve provider reasoning in same-agent history and validate tool calls."""

import json
import os
import time
import hashlib
from copy import deepcopy
import httpx
from pydantic import ValidationError
from tau2.data_model.message import AssistantMessage, UserMessage, ToolCall
from payment.runtime.interaction.tau.llm import wire_message
from payment.runtime.core.storage import append
from payment.runtime.interaction.transport.deadline import EpisodeTimeoutError


class ModelProtocolError(ValueError):
    """A role produced a model response that cannot be executed as a tool call."""

    def __init__(self, role, message):
        self.role = role
        super().__init__(message)


def parse_tool_calls(message, role):
    try:
        return [
            ToolCall(
                id=call["id"],
                name=call["function"]["name"],
                arguments=json.loads(call["function"]["arguments"]),
                requestor=role,
            )
            for call in (message.get("tool_calls") or [])
        ]
    except (json.JSONDecodeError, TypeError, KeyError, ValidationError) as exc:
        raise ModelProtocolError(role, "Model returned invalid tool-call arguments") from exc


class ReasoningEndpoint:
    def __init__(self, config, role, ledger_path):
        self.config = deepcopy(config)
        self.role = role
        self.ledger_path = ledger_path
        self.calls = []
        self.key = os.environ.get(config.get("api_key_env", ""), "") or config.get(
            "dummy_api_key", ""
        )
        self.client = httpx.Client(
            timeout=httpx.Timeout(config.get("timeout_seconds", 60)),
            trust_env=config.get("trust_env", True),
        )
        self.seed = 0
        self.provider_messages = {}
        self.pending_public_text = None
        self.delivery_events = []

    def encode_message(self, message, index):
        encoded = wire_message(message)
        saved = self.provider_messages.get(index)
        original = saved["provider"] if saved is not None else None
        if saved is not None and encoded != saved["wire"]:
            raise ValueError("Agent history differs from recorded provider message position")
        if original is not None and message.role == "assistant":
            # Kept only in this participant's endpoint, never in tau user messages.
            for key in ("reasoning_content", "reasoning"):
                if original.get(key) is not None:
                    encoded[key] = original[key]
            encoded["content"] = None if original.get("tool_calls") else original.get("content")
        return encoded

    def complete(self, messages, tools):
        if self.pending_public_text is not None:
            pending = self.pending_public_text
            # Native tau comes back to this actor after its tool receipts.
            assert any(
                m.role == "tool" for m in messages[pending["history_length"] + 1 :]
            ), "Deferred speech requires tool receipts"
            self.pending_public_text = None
            cls = AssistantMessage if self.role == "assistant" else UserMessage
            event = {
                "role": self.role,
                "phase": "delivered",
                "source_call_index": pending["source_call_index"],
                "content": pending["content"],
                "at_unix": time.time(),
            }
            self.delivery_events.append(event)
            append(self.ledger_path.parent / "public_delivery.jsonl", event)
            return cls(role=self.role, content=pending["content"])
        params = {
            "model": self.config["model"],
            "messages": [self.encode_message(m, i) for i, m in enumerate(messages)],
            "temperature": self.config.get("temperature", 0),
            "max_tokens": self.config.get("max_tokens", 2048),
            "stream": False,
        }
        if tools:
            params["tools"] = [t.openai_schema for t in tools]
            params["parallel_tool_calls"] = False
        if self.config.get("send_seed", False):
            params["seed"] = self.seed
        params.update(self.config.get("extra_body", {}))
        # Customer portal actions are sequential by protocol.  Keep this after
        # extra_body so a shared provider config cannot silently re-enable
        # parallel writes for the user simulator.
        if tools and self.role == "user":
            params["parallel_tool_calls"] = False
        row = {
            "role": self.role,
            "call_index": len(self.calls),
            "model": self.config["model"],
            "request": params,
            "started_at_unix": time.time(),
            "attempt": 0,
            "cost_usd": None,
        }
        started = time.monotonic()
        try:
            headers = {"Authorization": "Bearer " + self.key} if self.key else {}
            response = self.client.post(
                self.config["base_url"].rstrip("/") + "/chat/completions",
                headers=headers,
                json=params,
            )
            row["http_status"] = response.status_code
            row["raw_response"] = response.text
            if response.status_code >= 400:
                row["error_type"] = "INFRASTRUCTURE_HTTP"
                raise RuntimeError(f"{self.role} endpoint returned HTTP {response.status_code}")
            raw = response.json()
            row["response"] = raw
            row["usage"] = raw.get("usage")
            m = raw["choices"][0]["message"]
            calls = parse_tool_calls(m, self.role)
            content = m.get("content")
            if not calls and not content:
                row["error_type"] = "MODEL_EMPTY_MESSAGE"
                raise ModelProtocolError(self.role, "Model returned no text or tool calls")
            # tau2's half-duplex protocol routes tool messages to the environment.
            # Deliver the original text verbatim in a separate native turn after receipts.
            # This is pre-tool speech, not a newly generated success summary.
            if calls and content:
                if content.strip():
                    self.pending_public_text = {
                        "content": content,
                        "history_length": len(messages),
                        "source_call_index": len(self.calls),
                    }
                    row["normalization"] = "public_text_queued_after_tool_receipts"
                else:
                    row["normalization"] = "whitespace_only_content_removed"
                content = None
            cls = AssistantMessage if self.role == "assistant" else UserMessage
            result = cls(
                role=self.role,
                content=content,
                tool_calls=calls or None,
                usage=raw.get("usage"),
                cost=None,
            )
            if self.role == "assistant" and any(
                m.get(k) is not None for k in ("reasoning_content", "reasoning")
            ):
                self.provider_messages[len(messages)] = {
                    "provider": deepcopy(m),
                    "wire": wire_message(result),
                }
            return result
        except EpisodeTimeoutError as exc:
            row["error_type"] = "EPISODE_TIMEOUT"
            row["error_class"] = type(exc).__name__
            raise
        except httpx.HTTPError as exc:
            row["error_type"] = "INFRASTRUCTURE_TRANSPORT"
            row["error_class"] = type(exc).__name__
            raise RuntimeError(
                f"{self.role} endpoint transport error: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            row.setdefault("error_type", "MODEL_PROTOCOL")
            row["error_class"] = type(exc).__name__
            raise
        finally:
            row["duration_seconds"] = time.monotonic() - started
            if self.config.get("compact_api_ledger"):
                row["request_sha256"] = hashlib.sha256(
                    json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                row.pop("request", None)
                row.pop("raw_response", None)
            self.calls.append(row)
            append(self.ledger_path, row)

    def close(self):
        self.client.close()
