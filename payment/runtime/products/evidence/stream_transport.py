"""SSE transport only: no changes to message, model, sampling or tool semantics.

All raw chunks are durable. An incomplete stream is never delivered as a model
message. DONE plus a finish reason are required, including finish_reason=length.
"""

import asyncio
from contextlib import aclosing
import json
import time
import gzip
import httpx

from payment.runtime.interaction.transport.deadline import EpisodeTimeoutError, remaining_seconds


def assemble(chunks):
    msg = {"role": "assistant"}
    calls = {}
    finish = None
    usage = None
    model = None
    response_id = None
    for chunk in chunks:
        if chunk.get("error"):
            raise ValueError("Provider stream error")
        model = chunk.get("model", model)
        response_id = chunk.get("id", response_id)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("Multiple completions")
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]
            d = choice.get("delta", {})
            for key in ("content", "reasoning_content", "reasoning"):
                if d.get(key) is not None:
                    if not isinstance(d[key], str):
                        raise ValueError("Non-string delta")
                    msg[key] = msg.get(key, "") + d[key]
            for t in d.get("tool_calls", []) or []:
                dst = calls.setdefault(
                    t["index"],
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if t.get("id"):
                    dst["id"] = t["id"]
                for key in ("name", "arguments"):
                    dst["function"][key] += t.get("function", {}).get(key, "") or ""
    if finish is None:
        raise ValueError("Missing finish reason")
    msg.setdefault("content", None)
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return dict(
        id=response_id,
        model=model,
        choices=[dict(index=0, message=msg, finish_reason=finish)],
        usage=usage,
    )


async def post(client, url, body, log_dir, role, seq, compressed=False, *, deadline, **kwargs):
    # The admitted logical request stays in api_calls; this file records its wire transport.
    wire = {**body, "stream": True, "stream_options": {"include_usage": True}}
    path = log_dir / (f"stream_{role}_{seq:04d}.jsonl" + (".gz" if compressed else ""))
    chunks = []
    done = False
    opener = (
        (lambda: gzip.open(path, "xt", encoding="utf-8"))
        if compressed
        else (lambda: path.open("x"))
    )
    with opener() as f:

        def log(v):
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
            f.flush()

        log(dict(kind="request", url=url, request=wire, started_at=time.time()))
        try:
            # Unlike the per-read HTTP timeout, this timer is not reset by
            # keepalives or token chunks. Cancellation also interrupts waiting
            # for connection setup, headers, or an HTTP error response body.
            async with asyncio.timeout(remaining_seconds(deadline, role)):
                async with client.stream("POST", url, json=wire, **kwargs) as r:
                    remaining_seconds(deadline, role)
                    log(dict(kind="headers", status=r.status_code, at=time.time()))
                    if r.status_code >= 400:
                        raw = await r.aread()
                        remaining_seconds(deadline, role)
                        log(dict(kind="http_error", body=raw.decode(errors="replace")))
                        return httpx.Response(
                            r.status_code, content=raw, request=httpx.Request("POST", url)
                        )
                    async with aclosing(r.aiter_lines()) as lines:
                        async for line in lines:
                            # Buffered lines can arrive without yielding to the
                            # event loop; check the same deadline for those too.
                            remaining_seconds(deadline, role)
                            if not line.startswith("data:"):
                                continue
                            value = line[5:].strip()
                            if value == "[DONE]":
                                done = True
                                log(dict(kind="done", at=time.time()))
                                break
                            if value:
                                chunk = json.loads(value)
                                chunks.append(chunk)
                                log(dict(kind="chunk", chunk=chunk))
            remaining_seconds(deadline, role)
            if not done:
                raise ValueError("Missing DONE")
            response = httpx.Response(200, json=assemble(chunks), request=httpx.Request("POST", url))
            remaining_seconds(deadline, role)
            return response
        except TimeoutError as exc:
            log(dict(kind="episode_timeout", role=role, chunks_received=len(chunks), at=time.time()))
            raise EpisodeTimeoutError(role) from exc
        except (ValueError, KeyError) as exc:
            log(dict(kind="incomplete_stream", error=str(exc)))
            return httpx.Response(502, json={"error": str(exc)}, request=httpx.Request("POST", url))
        except httpx.HTTPError as exc:
            log(dict(kind="transport_error", error=type(exc).__name__))
            raise
