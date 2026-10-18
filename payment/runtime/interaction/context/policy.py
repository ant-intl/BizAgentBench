"""Evict knowledge receipts from the provider view, never from the trajectory."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time

PROTOCOL = {
    "id": "retrieval-context-1.0.0",
    "trigger": "explicit_provider_context_overflow",
    "roles": ["assistant"],
    "retrieval_tools": ["search_knowledge", "read_document"],
    "fraction": 0.25,
    "rounding": "ceil",
    "denominator": "currently_retained_reducible_retrieval_receipts",
    "selection": "oldest_first",
    "trajectory_preserved": True,
    "output_and_business_budgets_unchanged": True,
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def append(path, value):
    with Path(path).open("a") as stream:
        stream.write(canonical(value) + "\n")


def context_error(response):
    """Only explicit rejection of input/context size permits a changed request."""
    if response.status_code not in (400, 413, 422):
        return False
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        return False
    if not isinstance(data, dict) or data.get("choices"):
        return False
    # vLLM also uses a top-level ErrorResponse rather than an `error` envelope.
    error = data.get("error", data if data.get("object") == "error" else None)
    if isinstance(error, dict):
        if any(
            error.get(k)
            in (
                "context_length_exceeded",
                "context_window_exceeded",
                "input_context_length_exceeded",
            )
            for k in ("code", "type")
        ):
            return True
        message = error.get("message", "")
    else:
        message = error
    if not isinstance(message, str):
        return False
    # Do not infer overflow from a generic 413, an output limit, or a 502 body.
    patterns = (
        r"maximum context length (?:is|of)\s*\d+",
        r"(?:input|prompt|request)(?:.{0,100})exceeds? (?:the )?(?:model\S* )?(?:context|maximum model) (?:length|window)",
        r"(?:context length|context window)(?:.{0,60})(?:exceeded|exceeds|too long)",
        r"max_tokens.{0,50}max_model_len.{0,50}prompt",
    )
    return any(re.search(pattern, message, re.I) for pattern in patterns)


class ContextClient:
    """Wrap any synchronous completion client; each retry gets its own job ID.

    `params` is the endpoint's newly constructed wire request. Updating it keeps
    api_calls.jsonl equal to the final transmitted input. Original tau messages
    and provider reasoning are untouched. Rejected inputs are logged separately.
    """

    def __init__(self, client, role, ledger_path):
        self.client = client
        self.role = role
        self.root = Path(ledger_path).parent
        self.evicted = {}
        self.sequence = 0
        # State files are audit snapshots. A resumed episode must replay its
        # verified prefix and cached attempts; never apply a future snapshot.

    @property
    def state_path(self):
        return self.root / f"context_state_{self.role}.json"

    def apply_previous(self, messages):
        for index, record in self.evicted.items():
            if index >= len(messages) or fingerprint(messages[index]) != record["receipt_sha256"]:
                raise ValueError(
                    "Context recovery history mismatch; audit the saved prefix before resuming"
                )
            messages[index]["content"] = record["placeholder"]

    def candidates(self, messages):
        # Link by call ID and require one earlier call and one receipt. Text that
        # merely mentions a retrieval tool cannot make a receipt eligible.
        calls, receipts = {}, {}
        for index, message in enumerate(messages):
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    calls.setdefault(call.get("id"), []).append((index, call))
            elif message.get("role") == "tool":
                receipts.setdefault(message.get("tool_call_id"), []).append(index)
        candidates = []
        for call_id, indices in receipts.items():
            linked = calls.get(call_id, [])
            if not call_id or len(indices) != 1 or len(linked) != 1:
                continue
            index = indices[0]
            call_index, call = linked[0]
            if call_index >= index or index in self.evicted:
                continue
            name = call.get("function", {}).get("name")
            if name not in PROTOCOL["retrieval_tools"]:
                continue
            content = messages[index].get("content")
            if not isinstance(content, str) or not content:
                continue
            placeholder = (
                "[Knowledge retrieval output removed to fit the context window. "
                "Reissue the corresponding " + name + " call to recover it.]"
            )
            if len(content.encode()) <= len(placeholder.encode()):
                continue
            candidates.append((index, call_id, name, placeholder))
        return sorted(candidates)

    def post(self, url, json, **kwargs):
        self.sequence += 1
        if self.role not in PROTOCOL["roles"]:
            return self.client.post(url, json=json, **kwargs)
        wire = deepcopy(json)
        self.apply_previous(wire["messages"])
        recovery_count = 0
        while True:
            # The surrounding endpoint records exactly this request, even when
            # the transport subsequently raises instead of returning HTTP.
            json.clear()
            json.update(deepcopy(wire))
            response = self.client.post(url, json=deepcopy(wire), **kwargs)
            overflow = context_error(response)
            if overflow or recovery_count:
                append(
                    self.root / "context_attempts.jsonl",
                    dict(
                        protocol=PROTOCOL["id"],
                        role=self.role,
                        completion_index=self.sequence,
                        attempt=recovery_count,
                        request=wire,
                        request_sha256=fingerprint(wire),
                        http_status=response.status_code,
                        raw_response=response.text,
                        context_overflow=overflow,
                        at_unix=time.time(),
                    ),
                )
            if not overflow:
                return response
            choices = self.candidates(wire["messages"])
            count = math.ceil(len(choices) * PROTOCOL["fraction"])
            removed = []
            for index, call_id, name, placeholder in choices[:count]:
                receipt = wire["messages"][index]
                original = receipt["content"]
                # A replacement must actually reduce bytes; tiny empty/error
                # receipts cannot grow the request or cause a retry loop.
                if len(original.encode()) <= len(placeholder.encode()):
                    continue
                record = dict(
                    message_index=index,
                    tool_call_id=call_id,
                    tool_name=name,
                    receipt_sha256=fingerprint(receipt),
                    original_bytes=len(original.encode()),
                    placeholder=placeholder,
                )
                self.evicted[index] = record
                receipt["content"] = placeholder
                removed.append(record)
            event = dict(
                protocol=PROTOCOL["id"],
                role=self.role,
                completion_index=self.sequence,
                attempt=recovery_count,
                retained_retrieval_receipts=len(choices),
                requested_evictions=count,
                removed=removed,
                removed_count=len(removed),
                recoverable=bool(removed),
                at_unix=time.time(),
            )
            append(self.root / "context_events.jsonl", event)
            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(
                canonical(
                    dict(protocol=PROTOCOL, role=self.role, evicted=list(self.evicted.values()))
                )
                + "\n"
            )
            temp.replace(self.state_path)
            if not removed:
                # Nothing eligible remains. Preserve this rejected request and
                # let the runner classify a context limit, not a business error.
                return response
            recovery_count += 1

    def close(self):
        self.client.close()
