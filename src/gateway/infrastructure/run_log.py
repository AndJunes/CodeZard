"""What a run has already said, so a tab that comes back can catch up.

A generation streams for minutes and the browser holds the only copy of what it has seen. Lose
the connection — a reload, a laptop lid, a proxy giving up — and the work carries on
server-side with nobody able to watch it. `GET /runs/{id}` can already say WHERE a run is; this
is what lets a reconnecting tab see how it got there and then keep watching.

It is deliberately not a field of `Run`. `Run` is a frozen dataclass whose every move returns a
copy, which is exactly right for a state machine and exactly wrong for a buffer appended to a
hundred times during one generation.

Nothing is written to disk. TC-5 and DR-4 forbid carrying state between executions, and a log
that outlives the process would be carrying the most detailed state of all.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from gateway.domain.ports import RunLog

MAX_BYTES = 256 * 1024
"""How much of one run's stream is kept.

A real generation's event stream measured about 12 kB, so this is twenty times a normal run
and the cap only bites on something pathological. Past it the OLDEST chunks go: a reconnecting
tab cares most about what just happened, and the run's own state — the plan, the verdict, the
artifact id — lives in the `Run` and is never truncated.
"""

MAX_RUNS = 200
"""Runs with a log at once. Smaller than the store's own ceiling because a log costs more than
a run: eviction takes the least recently appended to."""


class InMemoryRunLog(RunLog):
    """Append on one side, replay-and-follow on the other.

    Both sides run in the same event loop — the orchestrator appends from the request that is
    generating, a reconnecting request reads — so an `asyncio.Event` is enough to wake readers
    and no thread-safety is needed or claimed.
    """

    def __init__(self, max_bytes: int = MAX_BYTES, max_runs: int = MAX_RUNS) -> None:
        self._chunks: dict[str, list[bytes]] = {}
        self._sizes: dict[str, int] = {}
        self._ended: dict[str, bool] = {}
        self._woken: dict[str, asyncio.Event] = {}
        self._max_bytes = max_bytes
        self._max_runs = max_runs

    # ── the writing side ─────────────────────────────────────────────────────

    def start(self, run_id: str) -> None:
        """A generation is beginning. Any earlier log for this run is replaced."""
        self._evict()
        self._chunks[run_id] = []
        self._sizes[run_id] = 0
        self._ended[run_id] = False
        self._woken[run_id] = asyncio.Event()

    def append(self, run_id: str, chunk: bytes) -> None:
        if run_id not in self._chunks:
            self.start(run_id)
        self._chunks[run_id].append(chunk)
        self._sizes[run_id] += len(chunk)
        while self._sizes[run_id] > self._max_bytes and len(self._chunks[run_id]) > 1:
            self._sizes[run_id] -= len(self._chunks[run_id].pop(0))
        self._wake(run_id)

    def end(self, run_id: str) -> None:
        """The generation is over, however it ended. Readers stop after draining."""
        if run_id in self._ended:
            self._ended[run_id] = True
            self._wake(run_id)

    # ── the reading side ─────────────────────────────────────────────────────

    def has(self, run_id: str) -> bool:
        return run_id in self._chunks

    def ended(self, run_id: str) -> bool:
        return self._ended.get(run_id, True)

    async def follow(self, run_id: str) -> AsyncIterator[bytes]:
        """Everything said so far, then everything said next, until the run ends.

        The cursor is per-reader, so two tabs watching the same run do not consume each
        other's events — and a reader that started late still gets the whole log first.
        """
        cursor = 0
        while True:
            chunks = self._chunks.get(run_id)
            if chunks is None:
                return
            if cursor < len(chunks):
                for chunk in chunks[cursor:]:
                    yield chunk
                cursor = len(chunks)
                continue
            if self._ended.get(run_id, True):
                return
            waiter = self._woken.get(run_id)
            if waiter is None:
                return
            await waiter.wait()

    # ── housekeeping ─────────────────────────────────────────────────────────

    def _wake(self, run_id: str) -> None:
        """Release everyone waiting, then re-arm.

        Replacing the Event rather than clearing it: a reader woken by `set()` may not have
        been scheduled yet when `clear()` runs, and would then wait on an Event that already
        fired. Handing out a fresh one leaves the old waiters holding the one that is set.
        """
        waiter = self._woken.get(run_id)
        if waiter is not None:
            waiter.set()
        self._woken[run_id] = asyncio.Event()

    def _evict(self) -> None:
        while len(self._chunks) >= self._max_runs:
            oldest = next(iter(self._chunks))
            self._forget(oldest)

    def _forget(self, run_id: str) -> None:
        self._chunks.pop(run_id, None)
        self._sizes.pop(run_id, None)
        self._ended.pop(run_id, None)
        waiter = self._woken.pop(run_id, None)
        if waiter is not None:
            waiter.set()  # nobody should be left waiting on a log that no longer exists

    def __len__(self) -> int:
        return len(self._chunks)
