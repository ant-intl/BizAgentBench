"""Durable transport for OpenAI-compatible assistant and customer models."""

from copy import deepcopy
import time

import httpx

from payment.runtime.interaction.context.policy import ContextClient
from payment.runtime.interaction.transport.deadline import remaining_seconds
from payment.runtime.interaction.transport.endpoint_base import ReasoningEndpoint
from tools.transport_support import CachedResponseBase, append, digest, empty_success_message


class CachedCompatible(CachedResponseBase):
    def __init__(self, config, role, folder, deadline):
        self.config = deepcopy(config)
        super().__init__(config, role, folder, deadline)

    def post(self, url, json, **kwargs):
        remaining_seconds(self.deadline, self.role)
        expected = self.config["base_url"].rstrip("/") + "/chat/completions"
        if url != expected or json.get("model") != self.config["model"]:
            raise ValueError("ENDPOINT_CONFIG_MISMATCH")
        self.seq += 1
        slot = self.cache / f"{self.seq:05d}"
        slot.mkdir(parents=True, exist_ok=True)
        request_path = slot / (
            "request.json" if (slot / "request.json").exists() else "request.json.gz"
        )
        response_path = slot / (
            "response.json" if (slot / "response.json").exists() else "response.json.gz"
        )
        if request_path.exists():
            if digest(self._read_cache(request_path)) != digest(json):
                raise RuntimeError("REPLAY_REQUEST_MISMATCH")
        else:
            self._write_cache(request_path, json)
        if response_path.exists():
            saved = self._read_cache(response_path)
            if saved["status"] == 200 and empty_success_message(saved["body"]):
                response_path.unlink()
            else:
                append(
                    self.root / "cache_replay.jsonl",
                    {"role": self.role, "seq": self.seq, "request_sha256": digest(json)},
                )
                remaining_seconds(self.deadline, self.role)
                return httpx.Response(
                    saved["status"], json=saved["body"], request=httpx.Request("POST", url)
                )
        attempt = 0
        while True:
            remaining_seconds(self.deadline, self.role)
            attempt += 1
            try:
                response = self.stream.post(url, json=deepcopy(json), **kwargs)
                remaining_seconds(self.deadline, self.role)
                if response.status_code not in (408, 429, 500, 502, 503, 504):
                    body = response.json()
                    if response.status_code == 200:
                        reported = body.get("model")
                        allowed = {self.config["model"], self.config.get("response_model")}
                        if reported and reported not in allowed:
                            raise RuntimeError("PROVIDER_MODEL_MISMATCH")
                        if empty_success_message(body):
                            reason = {"empty_message": True}
                        else:
                            self._write_cache(response_path, {"status": 200, "body": body})
                            remaining_seconds(self.deadline, self.role)
                            return response
                    else:
                        if response.status_code in (400, 413, 422):
                            self._write_cache(
                                response_path, {"status": response.status_code, "body": body}
                            )
                        return response
                else:
                    reason = {"status": response.status_code}
            except httpx.TransportError as exc:
                reason = {"exception": type(exc).__name__}
            append(
                self.root / "provider_retries.jsonl",
                {
                    "role": self.role,
                    "seq": self.seq,
                    "attempt": attempt,
                    "reason": reason,
                    "request_sha256": digest(json),
                    "bank_tools_executed": False,
                    "time": time.time(),
                },
            )
            remaining = remaining_seconds(self.deadline, self.role)
            time.sleep(min(15, attempt * 2, remaining))


def endpoint(config, role, folder, deadline):
    result = ReasoningEndpoint(config, role, folder / "api_calls.jsonl")
    result.client.close()
    result.client = ContextClient(
        CachedCompatible(config, role, folder, deadline), role, folder / "api_calls.jsonl"
    )
    return result
