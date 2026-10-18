"""Stream transport for independently configured model roles."""

import asyncio
from pathlib import Path

from payment.runtime.interaction.transport.deadline import remaining_seconds
from payment.runtime.products.evidence.stream_transport import post


class RoleStreamClient:
    def __init__(self, client, ledger_path, role, compress_logs=False, *, deadline):
        self.client = client
        self.path = Path(ledger_path).parent / "streams"
        self.path.mkdir(exist_ok=True)
        self.role = role
        self.seq = 0
        self.compress_logs = compress_logs
        self.deadline = deadline
        # Keep HTTP connections on one event loop for this participant. The
        # synchronous orchestrator runs it directly, without a request worker
        # that could continue reading after a timeout has been reported.
        self.loop = asyncio.new_event_loop()
        self.closed = False

    def post(self, url, json, **kwargs):
        remaining_seconds(self.deadline, self.role)
        self.seq += 1
        return self.loop.run_until_complete(
            post(
                self.client,
                url,
                json,
                self.path,
                self.role,
                self.seq,
                compressed=self.compress_logs,
                deadline=self.deadline,
                **kwargs,
            )
        )

    def close(self):
        if not self.closed:
            try:
                self.loop.run_until_complete(self.client.aclose())
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.run_until_complete(self._finish_cleanup())
            finally:
                # Do not join the default executor here: an OS DNS lookup
                # cannot be cancelled, even though its awaiting HTTP task has
                # been. Let that lookup finish without holding the episode
                # worker; it cannot resume a request on this closed loop.
                self.loop.close()
                self.closed = True

    async def _finish_cleanup(self):
        current = asyncio.current_task()
        # Closing one generator can schedule finalizers for nested HTTPX
        # iterators. Drain those before closing their event loop.
        while pending := asyncio.all_tasks() - {current}:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
