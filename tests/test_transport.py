"""Local HTTP regressions for absolute episode deadlines and SSE cancellation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from tools import evaluate
from tools.payment_endpoint import CachedCompatible
from payment.runtime.interaction.domain import DOMAIN, register
from payment.runtime.interaction.transport.deadline import EpisodeTimeoutError


class StreamingServer(ThreadingHTTPServer):
    """Simulate stalled and trickling providers without any external API calls."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), StreamingHandler)
        self.stop = threading.Event()
        self.requests = []
        self.disconnected = threading.Event()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_port}/v1"


class StreamingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def wait_for_disconnect(self):
        while not self.server.stop.wait(0.01):
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable and not self.connection.recv(1):
                self.server.disconnected.set()
                return

    def send_chunk(self, value):
        self.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        mode = body.get("mode", body["model"])
        try:
            if mode == "headers_stall":
                self.wait_for_disconnect()
                return
            status = 503 if mode in {"error_stall", "retry"} else 200
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            if mode in {"body_stall", "error_stall"}:
                self.wait_for_disconnect()
                return
            if mode == "retry":
                self.wfile.write(b'{"error":"temporary failure"}')
                self.wfile.flush()
                return
            if mode in {"trickle", "heartbeat"}:
                while not self.server.stop.wait(0.01):
                    if mode == "heartbeat":
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    else:
                        self.send_chunk({"choices": [{"delta": {"content": "a"}}]})
                return
            self.send_chunk({
                "id": "local-completion",
                "choices": [{"delta": {"reasoning_content": "Check "}}],
            })
            self.send_chunk({"choices": [{"delta": {"reasoning_content": "terms."}}]})
            if mode == "tool":
                self.send_chunk({"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "clock_call", "function": {
                        "name": "get_clock", "arguments": "{",
                    },
                }]}}]})
                self.send_chunk({"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "function": {"arguments": "}"},
                }]}}]})
            else:
                self.send_chunk({"choices": [{"delta": {"content": "Please help with my payment."}}]})
            self.send_chunk({"choices": [{"delta": {}, "finish_reason": "stop"}],
                             "usage": {"total_tokens": 5}})
            if mode != "incomplete":
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.server.disconnected.set()


@contextmanager
def local_server():
    server = StreamingServer()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def config(server, model="normal"):
    return {"model": model, "base_url": server.base_url, "timeout_seconds": 5, "trust_env": False}


def read_stream(path):
    with gzip.open(path, "rt") as handle:
        return [json.loads(line) for line in handle]


class TransportTests(unittest.TestCase):
    def test_stream_deadline_interrupts_trickles_keepalives_and_silent_reads(self):
        for mode in ("trickle", "heartbeat", "headers_stall", "body_stall", "error_stall"):
            with self.subTest(mode=mode), local_server() as server, tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = CachedCompatible(config(server), "assistant", root, time.monotonic() + 10)
                # Start measuring after transport setup so slow CI startup does
                # not accidentally turn this into a pre-request expiry test.
                started = time.monotonic()
                client.deadline = client.stream.deadline = started + 0.3
                try:
                    with self.assertRaises(EpisodeTimeoutError) as caught:
                        client.post(server.base_url + "/chat/completions", {"model": "normal", "mode": mode})
                    self.assertEqual(caught.exception.role, "assistant")
                    self.assertLess(time.monotonic() - started, 1.2)
                    self.assertTrue(server.disconnected.wait(0.8), "cancelled stream socket stayed open")
                    self.assertEqual(len(server.requests), 1)
                    self.assertFalse(list((root / "api_cache").rglob("response.*")))
                    rows = read_stream(next((root / "streams").glob("*.gz")))
                    self.assertEqual(rows[-1]["kind"], "episode_timeout")
                    if mode == "trickle":
                        self.assertGreater(rows[-1]["chunks_received"], 0)
                    self.assertFalse((root / "provider_retries.jsonl").exists())
                finally:
                    client.close()
                self.assertTrue(client.stream.closed)
                self.assertTrue(client.stream.client.is_closed)
                self.assertTrue(client.stream.loop.is_closed())
                self.assertFalse(asyncio.all_tasks(client.stream.loop))

    def test_dns_lookup_does_not_hold_episode_close_or_resume_http_request(self):
        with local_server() as server, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lookup_started = threading.Event()
            release_lookup = threading.Event()
            lookup_thread = []
            original_getaddrinfo = socket.getaddrinfo

            def delayed_lookup(host, port, *args, **kwargs):
                lookup_thread.append(threading.current_thread())
                lookup_started.set()
                release_lookup.wait(2)
                return original_getaddrinfo("127.0.0.1", port, *args, **kwargs)

            settings = config(server)
            settings["base_url"] = server.base_url.replace("127.0.0.1", "deadline.invalid")
            client = CachedCompatible(settings, "assistant", root, time.monotonic() + 10)
            started = time.monotonic()
            client.deadline = client.stream.deadline = started + 0.3
            try:
                with patch("socket.getaddrinfo", side_effect=delayed_lookup):
                    with self.assertRaises(EpisodeTimeoutError):
                        client.post(settings["base_url"] + "/chat/completions", {"model": "normal"})
                    self.assertTrue(lookup_started.is_set())
                    client.close()
                    self.assertLess(time.monotonic() - started, 1.2)
                    self.assertTrue(client.stream.loop.is_closed())
                    self.assertFalse(asyncio.all_tasks(client.stream.loop))
            finally:
                release_lookup.set()
                client.close()
                for thread in lookup_thread:
                    thread.join(timeout=1)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(server.requests, [], "cancelled DNS lookup resumed an HTTP request")

    def test_successful_stream_preserves_tool_reasoning_usage_and_cache_replay(self):
        with local_server() as server, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = {"model": "normal", "mode": "tool"}
            client = CachedCompatible(config(server), "assistant", root, time.monotonic() + 5)
            try:
                response = client.post(server.base_url + "/chat/completions", deepcopy(body))
                saved = response.json()
                message = saved["choices"][0]["message"]
                self.assertEqual(message["reasoning_content"], "Check terms.")
                self.assertEqual(message["tool_calls"][0]["function"], {"name": "get_clock", "arguments": "{}"})
                self.assertEqual(saved["usage"]["total_tokens"], 5)
                self.assertEqual(body, {"model": "normal", "mode": "tool"})
                # A second request on the same event loop must also succeed.
                self.assertEqual(client.post(server.base_url + "/chat/completions", body).json(), saved)
            finally:
                client.close()
            replay = CachedCompatible(config(server), "assistant", root, time.monotonic() + 5)
            try:
                self.assertEqual(replay.post(server.base_url + "/chat/completions", body).json(), saved)
                self.assertEqual(len(server.requests), 2)
                replay.deadline = time.monotonic() - 1
                with self.assertRaises(EpisodeTimeoutError):
                    replay.post(server.base_url + "/chat/completions", body)
                self.assertEqual(len(server.requests), 2)
            finally:
                replay.close()

    def test_expired_episode_makes_no_request_and_creates_no_success_cache(self):
        with local_server() as server, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = CachedCompatible(config(server), "user", root, time.monotonic() - 1)
            try:
                with self.assertRaises(EpisodeTimeoutError):
                    client.post(server.base_url + "/chat/completions", {"model": "normal"})
                self.assertEqual(server.requests, [])
                self.assertEqual(list((root / "streams").iterdir()), [])
            finally:
                client.close()

    def test_http_failure_and_incomplete_stream_recovery_share_episode_budget(self):
        for mode in ("retry", "incomplete"):
            with self.subTest(mode=mode), local_server() as server, tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = CachedCompatible(config(server), "assistant", root, time.monotonic() + 10)
                started = time.monotonic()
                client.deadline = client.stream.deadline = started + 0.3
                try:
                    with self.assertRaises(EpisodeTimeoutError):
                        client.post(server.base_url + "/chat/completions", {"model": "normal", "mode": mode})
                    self.assertLess(time.monotonic() - started, 1.2)
                    self.assertEqual(len(server.requests), 1)
                    self.assertTrue((root / "provider_retries.jsonl").is_file())
                    self.assertFalse(list((root / "api_cache").rglob("response.*")))
                finally:
                    client.close()

    def test_concurrent_episodes_have_independent_deadlines(self):
        with local_server() as server, tempfile.TemporaryDirectory() as tmp:
            def run(role, mode, seconds):
                root = Path(tmp) / role
                root.mkdir()
                client = CachedCompatible(config(server), role, root, time.monotonic() + seconds)
                try:
                    return client.post(server.base_url + "/chat/completions", {"model": "normal", "mode": mode}).status_code
                finally:
                    client.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                slow = pool.submit(run, "user", "trickle", 0.3)
                fast = pool.submit(run, "assistant", "normal", 5)
                with self.assertRaises(EpisodeTimeoutError):
                    slow.result(timeout=2)
                self.assertEqual(fast.result(timeout=2), 200)


class EpisodeTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tau2.registry import registry

        if DOMAIN not in registry.get_info().domains:
            register()

    def test_both_roles_timeout_with_scored_state_and_partial_receipts(self):
        for role in ("assistant", "user"):
            with self.subTest(role=role), local_server() as server, tempfile.TemporaryDirectory() as tmp:
                settings = {
                    "agent": config(server, "trickle" if role == "assistant" else "normal"),
                    "user": config(server, "trickle" if role == "user" else "normal"),
                    "episode_timeout_seconds": 1,
                    "max_errors": 16,
                    "seed": 0,
                }
                root = Path(tmp)
                result = evaluate.run_case("case_031", settings, root)
                summary = evaluate.summarize(root, [{"id": "case_031", "number": 31}])
                self.assertEqual(result["termination"], "timeout")
                self.assertEqual(result["classification"], "wall_clock_limit")
                self.assertEqual(result["timeout_role"], role)
                self.assertEqual(result["reward"], 0)
                self.assertEqual(summary["evaluated"], 1)
                self.assertEqual(summary["strict_successes"], 0)
                self.assertEqual(result["actual_tool_calls"], 0)
                self.assertLess(result["duration_seconds"], 2.5)
                folder = root / "case_031__trial0"
                self.assertTrue((folder / "trajectory_partial.json").exists())
                self.assertTrue((folder / "final_world.json").exists())
                calls = [json.loads(line) for line in (folder / "api_calls.jsonl").read_text().splitlines()]
                self.assertEqual(calls[-1]["error_type"], "EPISODE_TIMEOUT")
                self.assertEqual(calls[-1]["role"], role)
                self.assertTrue(server.disconnected.wait(0.8))

    def test_expiry_after_model_response_does_not_execute_pending_tools(self):
        with local_server() as server, tempfile.TemporaryDirectory() as tmp:
            settings = {
                "agent": config(server, "tool"),
                "user": config(server, "normal"),
                "episode_timeout_seconds": 0.7,
                "max_errors": 16,
                "seed": 0,
            }
            root = Path(tmp)
            original_write = evaluate.write
            delayed = []

            def checkpoint(path, value):
                original_write(path, value)
                if path.name == "trajectory_live.json" and value[-1].get("tool_calls") and not delayed:
                    delayed.append(True)
                    time.sleep(settings["episode_timeout_seconds"])

            # Simulate a checkpoint crossing the deadline after a completed
            # model response but before its requested business action executes.
            with patch("tools.evaluate.write", side_effect=checkpoint):
                result = evaluate.run_case("case_031", settings, root)
            self.assertTrue(delayed)
            self.assertEqual(result["termination"].lower(), "timeout")
            self.assertEqual(result["classification"], "wall_clock_limit")
            self.assertEqual(result["actual_tool_calls"], 0)
            folder = root / "case_031__trial0"
            self.assertTrue((folder / "unexecuted_message.json").exists())
            self.assertTrue((folder / "trajectory.json").exists())
            self.assertEqual(result["reward"], float(result["verification"]["strict_success"]))


if __name__ == "__main__":
    unittest.main()
