"""Atomic result files and response caching for resumable evaluations."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(value):
    content = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return hashlib.sha256(content).hexdigest()


def append(path, value):
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def empty_success_message(body):
    choices = body.get("choices") or []
    if not choices:
        return True
    message = choices[0].get("message") or {}
    return not message.get("content") and not (message.get("tool_calls") or [])


class CachedResponseBase:
    def __init__(self, config, role, folder, deadline):
        import httpx
        from payment.runtime.interaction.transport.endpoint import RoleStreamClient

        self.role = role
        self.root = folder
        self.deadline = deadline
        self.cache = folder / "api_cache" / role
        self.cache.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        proxy = config.get("proxy")
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(config["timeout_seconds"], connect=30),
            trust_env=False if proxy else config.get("trust_env", False),
            follow_redirects=False,
            **({"proxy": proxy} if proxy else {}),
        )
        self.stream = RoleStreamClient(
            client, folder / "api_calls.jsonl", role, compress_logs=True, deadline=deadline
        )
        previous = list((folder / "streams").glob(f"stream_{role}_*.jsonl*"))
        self.stream.seq = max(
            [int(path.name.split(".")[0].rsplit("_", 1)[1]) for path in previous] or [0]
        )

    @staticmethod
    def _read_cache(path):
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        return read(path)

    @staticmethod
    def _write_cache(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=4) as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        temporary.replace(path)

    def close(self):
        self.stream.close()
