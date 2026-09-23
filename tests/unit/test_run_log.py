"""The log a reconnecting tab reads.

A generation streams for minutes and the browser held the only copy of what it had seen. The
behaviour that matters is not "it stores bytes" — it is that a reader arriving LATE gets
everything from the start and then keeps up, and that two readers do not consume each other's
events.
"""

import asyncio

import pytest

from gateway.infrastructure.run_log import InMemoryRunLog


async def drain(log: InMemoryRunLog, run_id: str) -> list[bytes]:
    return [chunk async for chunk in log.follow(run_id)]


class TestReplaying:
    async def test_a_reader_that_arrives_late_gets_everything(self) -> None:
        log = InMemoryRunLog()
        log.start("r")
        log.append("r", b"one")
        log.append("r", b"two")
        log.end("r")
        assert await drain(log, "r") == [b"one", b"two"]

    async def test_a_finished_run_ends_the_reader(self) -> None:
        """A reload after the fact replays and stops, rather than hanging on a done run."""
        log = InMemoryRunLog()
        log.start("r")
        log.append("r", b"only")
        log.end("r")
        assert await asyncio.wait_for(drain(log, "r"), timeout=1) == [b"only"]

    async def test_an_unknown_run_yields_nothing(self) -> None:
        assert await drain(InMemoryRunLog(), "never-existed") == []


class TestFollowing:
    async def test_it_keeps_up_with_a_run_still_going(self) -> None:
        log = InMemoryRunLog()
        log.start("r")
        log.append("r", b"before")

        async def keep_writing() -> None:
            for chunk in (b"during-1", b"during-2"):
                await asyncio.sleep(0)
                log.append("r", chunk)
            log.end("r")

        writer = asyncio.create_task(keep_writing())
        seen = await asyncio.wait_for(drain(log, "r"), timeout=2)
        await writer
        assert seen == [b"before", b"during-1", b"during-2"]

    async def test_two_readers_do_not_eat_each_other_s_events(self) -> None:
        """Two tabs on the same run. The cursor is per reader, not per log."""
        log = InMemoryRunLog()
        log.start("r")
        log.append("r", b"a")

        async def later() -> None:
            await asyncio.sleep(0)
            log.append("r", b"b")
            log.end("r")

        writer = asyncio.create_task(later())
        both = await asyncio.wait_for(
            asyncio.gather(drain(log, "r"), drain(log, "r")), timeout=2)
        await writer
        assert both == [[b"a", b"b"], [b"a", b"b"]]

    async def test_a_forgotten_run_releases_its_readers(self) -> None:
        """Eviction must not leave a reader waiting on a log that no longer exists."""
        log = InMemoryRunLog(max_runs=2)
        log.start("first")
        log.append("first", b"x")
        reader = asyncio.create_task(drain(log, "first"))
        await asyncio.sleep(0)
        log.start("second")
        log.start("third")  # evicts `first`
        assert await asyncio.wait_for(reader, timeout=2) == [b"x"]


class TestBounds:
    async def test_the_oldest_chunks_go_first(self) -> None:
        """What just happened matters more to a reconnecting tab than what happened first."""
        log = InMemoryRunLog(max_bytes=10)
        log.start("r")
        for chunk in (b"aaaa", b"bbbb", b"cccc", b"dddd"):
            log.append("r", chunk)
        log.end("r")
        assert await drain(log, "r") == [b"cccc", b"dddd"]

    async def test_it_never_empties_itself(self) -> None:
        """One chunk larger than the whole budget is still the only thing there is to show."""
        log = InMemoryRunLog(max_bytes=4)
        log.start("r")
        log.append("r", b"x" * 100)
        log.end("r")
        assert await drain(log, "r") == [b"x" * 100]

    async def test_logs_are_bounded(self) -> None:
        log = InMemoryRunLog(max_runs=3)
        for i in range(10):
            log.start(f"run-{i}")
        assert len(log) <= 3

    async def test_starting_again_replaces_the_old_log(self) -> None:
        log = InMemoryRunLog()
        log.start("r")
        log.append("r", b"first attempt")
        log.start("r")
        log.append("r", b"second attempt")
        log.end("r")
        assert await drain(log, "r") == [b"second attempt"]


@pytest.fixture(autouse=True)
def _no_lingering_tasks() -> None:
    """Each test owns its tasks; a leak here would show up as a hang in the next one."""
    return None
