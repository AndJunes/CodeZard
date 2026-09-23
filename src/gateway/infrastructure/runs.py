"""Runs kept in this process, for as long as this process lives.

The sibling of ``InMemoryServiceRegistry``, and the same shape for the same reason: the
application layer asks a port, and only this file knows there is a dictionary behind it.

On the PRD. TC-5 and DR-4 forbid carrying state between executions. Nothing here is written
anywhere: a restart loses every run, and so does an hour of silence. That is the intended
reading of the rule rather than an exception to it — the alternative, keeping the state in the
browser, is what let a page open the backend agent by editing a JSON field.
"""

from __future__ import annotations

import asyncio
import time

from gateway.domain.exceptions import RunNotFoundError
from gateway.domain.ports import RunStore
from gateway.domain.runs import RUN_TTL_SECONDS, Run

MAX_RUNS = 500
"""A ceiling, because a dictionary nobody bounds is a slow memory leak wearing a hat.

Reached only by an instance under real load, and by then the oldest run in the map is one
somebody abandoned long ago: eviction takes the oldest first and never the newest.
"""


class InMemoryRunStore(RunStore):
    """Expiring, bounded, and safe to share across requests.

    The lock is not optional. Two requests for the same run arrive concurrently as a matter
    of course — a reconnecting tab while a generation is still streaming — and the eviction
    sweep walks the same dictionary those requests are writing to.
    """

    def __init__(self, ttl_seconds: float = RUN_TTL_SECONDS, max_runs: int = MAX_RUNS) -> None:
        self._runs: dict[str, Run] = {}
        self._ttl = ttl_seconds
        self._max = max_runs
        self._lock = asyncio.Lock()

    async def put(self, run: Run) -> Run:
        async with self._lock:
            self._sweep()
            self._runs[run.id] = run
            # Insertion order is not update order: a run already present keeps its original
            # position, so it must be moved to the end or a long, healthy run would be
            # evicted before a short abandoned one.
            self._runs[run.id] = self._runs.pop(run.id)
            while len(self._runs) > self._max:
                self._runs.pop(next(iter(self._runs)))
            return run

    async def get(self, run_id: str) -> Run:
        async with self._lock:
            self._sweep()
            run = self._runs.get(run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            return run

    def _sweep(self) -> None:
        """Drop what expired. Called under the lock, on every access, so there is no task to
        cancel at shutdown and no clock running when nothing is happening."""
        cutoff = time.monotonic() - self._ttl
        expired = [run_id for run_id, run in self._runs.items() if run.updated_at < cutoff]
        for run_id in expired:
            del self._runs[run_id]

    def __len__(self) -> int:
        return len(self._runs)
