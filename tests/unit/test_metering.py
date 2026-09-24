"""Reading what a run consumed out of the agent's stream, and charging for it.

This is the join between the agent and the ledger, and the place a mistake is most expensive:
everything here decides what somebody is billed.
"""

import json

import pytest

from gateway.application.orchestration import StreamScan
from gateway.domain.billing import Money, Usage
from gateway.domain.runs import Run, RunState


def sse(**event: object) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"


def done(**changes: object) -> bytes:
    return sse(
        type="done",
        project={"id": "a" * 24},
        cost={
            "text": "$0.0123",
            "free": False,
            "usage": {"tokens": 12_000, "calls": 5, "cost_usd": 0.0123, "simulated": False},
        },
        **changes,
    )


class TestStreamScan:
    def test_it_reads_the_artifact_and_the_usage_from_the_done_event(self) -> None:
        scan = StreamScan()
        scan.feed(done())

        assert scan.artifact == "a" * 24
        assert scan.usage.tokens == 12_000
        assert scan.usage.calls == 5
        assert scan.usage.cost == Money.parse("0.0123")

    def test_an_event_split_across_two_chunks_still_reads(self) -> None:
        """SSE routinely splits an event, and the half that arrives second is the half with
        the numbers in it."""
        payload = done()
        scan = StreamScan()
        scan.feed(payload[:30])
        assert scan.artifact == ""
        scan.feed(payload[30:])

        assert scan.artifact == "a" * 24
        assert scan.usage.tokens == 12_000

    def test_the_artifact_step_is_the_fallback_for_a_stream_cut_short(self) -> None:
        """Its `detail` is null and the id only appears inside a sentence written for a
        person — usable when `done` never arrives, and not before."""
        scan = StreamScan()
        scan.feed(sse(type="step", name="artifact", summary=f"listo con id {'b' * 24} en disco"))

        assert scan.artifact == "b" * 24
        assert scan.usage.is_empty

    def test_the_done_event_overrides_the_fallback(self) -> None:
        scan = StreamScan()
        scan.feed(sse(type="step", name="artifact", summary=f"id {'b' * 24}"))
        scan.feed(done())

        assert scan.artifact == "a" * 24

    def test_it_never_reads_the_usage_out_of_the_sentence(self) -> None:
        """`text` is written for a person and is free to change wording. A billing input
        parsed out of a sentence is one that will one day be parsed wrong."""
        scan = StreamScan()
        scan.feed(
            sse(type="done", project={"id": "a" * 24}, cost={"text": "$99.99", "free": False})
        )

        assert scan.usage.is_empty

    def test_a_simulated_run_is_marked_as_one(self) -> None:
        scan = StreamScan()
        scan.feed(
            sse(
                type="done",
                project={"id": "a" * 24},
                cost={
                    "simulated": True,
                    "usage": {"tokens": 0, "calls": 0, "cost_usd": 0.0, "simulated": True},
                },
            )
        )
        assert scan.usage.simulated

    @pytest.mark.parametrize(
        "noise",
        [
            b"event: ping\n",
            b"data: not json\n",
            b"data: [1,2,3]\n",
            b": a comment\n",
            b"\n",
        ],
    )
    def test_noise_in_the_stream_is_ignored(self, noise: bytes) -> None:
        scan = StreamScan()
        scan.feed(noise)
        scan.feed(done())

        assert scan.artifact == "a" * 24

    def test_a_stream_with_no_newline_cannot_grow_without_bound(self) -> None:
        scan = StreamScan()
        for _ in range(50):
            scan.feed(b"data: " + b"x" * 1_000)

        assert len(scan._tail) <= 8_192

    def test_an_incomplete_last_line_is_not_read_until_it_finishes(self) -> None:
        scan = StreamScan()
        scan.feed(done().rstrip(b"\n"))
        # No trailing newline, so the event is still the tail and must not have been read.
        assert scan.artifact == ""
        scan.feed(b"\n")
        assert scan.artifact == "a" * 24


class TestWhatARunRemembers:
    def test_a_run_records_who_pays_at_the_moment_it_starts(self) -> None:
        """Never from a later request: taking the account from whoever asks for the
        generation would let one signed-in caller spend another's balance by naming their
        run id."""
        run = Run.start("an idea", "G" + "A" * 55)
        assert run.account == "G" + "A" * 55
        assert run.as_json()["account"] == "G" + "A" * 55

    def test_a_gateway_that_is_not_charging_leaves_it_empty(self) -> None:
        assert Run.start("an idea").account == ""

    def test_what_it_was_charged_is_recorded_without_moving_it(self) -> None:
        """The state is already final by then; billing is not a transition."""
        run = Run.start("an idea", "G" + "A" * 55).describing()
        billed = run.billed(4_200)

        assert billed.charged == 4_200
        assert billed.state is run.state
        assert billed.as_json()["charged"] == 4_200

    def test_an_unbilled_run_says_zero_rather_than_nothing(self) -> None:
        assert Run.start("x").as_json()["charged"] == 0


class FakeMeter:
    """Records what it was asked and answers however the test wants."""

    def __init__(self, entry: object = None, refuse: Exception | None = None) -> None:
        self.entry = entry
        self.refuse = refuse
        self.authorized: list[str] = []
        self.charged: list[tuple[str, str, Usage]] = []

    async def authorize(self, account: str, needed: int = 0) -> object:
        self.authorized.append(account)
        if self.refuse is not None:
            raise self.refuse
        return None

    async def charge(self, account: str, run_id: str, usage: Usage) -> object:
        self.charged.append((account, run_id, usage))
        if self.refuse is not None:
            raise self.refuse
        return self.entry


class Entry:
    def __init__(self, tokens: int) -> None:
        self.tokens = tokens


class TestChargingARun:
    """`RunOrchestrator._charge` on its own: the stream and the agents are somebody else's
    tests, and what matters here is which runs end up debited."""

    @staticmethod
    def orchestrator(meter: FakeMeter | None):  # type: ignore[no-untyped-def]
        from gateway.application.orchestration import RunOrchestrator
        from gateway.config.settings import OrchestrationSettings

        return RunOrchestrator(
            None,
            None,
            OrchestrationSettings(),
            None,  # type: ignore[arg-type]
            meter=meter,
        )

    async def test_a_delivered_run_is_charged_and_remembers_how_much(self) -> None:
        meter = FakeMeter(Entry(-4_200))
        run = Run.start("x", "G" + "A" * 55)
        usage = Usage(tokens=12_000, calls=5, cost=Money.parse("0.0123"))

        billed = await self.orchestrator(meter)._charge(run, usage)

        assert [account for account, _, _ in meter.charged] == ["G" + "A" * 55]
        assert billed.charged == 4_200

    async def test_a_gateway_that_is_not_charging_debits_nobody(self) -> None:
        run = Run.start("x")
        billed = await self.orchestrator(None)._charge(run, Usage(tokens=12_000, calls=5))
        assert billed.charged == 0

    async def test_a_run_with_no_account_is_not_charged(self) -> None:
        meter = FakeMeter(Entry(-1))
        await self.orchestrator(meter)._charge(Run.start("x"), Usage(tokens=1, calls=1))
        assert meter.charged == []

    async def test_a_run_that_consumed_nothing_is_not_charged(self) -> None:
        meter = FakeMeter(Entry(-1))
        await self.orchestrator(meter)._charge(Run.start("x", "G" + "A" * 55), Usage())
        assert meter.charged == []

    async def test_a_ledger_that_cannot_be_written_does_not_fail_the_run(self) -> None:
        """The project exists and the caller is watching it arrive. A ledger we could not
        write is ours to notice in the logs, not a reason to turn a finished generation into
        an error."""
        meter = FakeMeter(refuse=RuntimeError("the disk is full"))
        run = Run.start("x", "G" + "A" * 55)

        billed = await self.orchestrator(meter)._charge(run, Usage(tokens=12_000, calls=5))

        assert billed.charged == 0
        assert billed.state is RunState.IDEA

    async def test_a_charge_of_nothing_leaves_the_run_unmarked(self) -> None:
        """`charge` answers None for a run that owed nothing; the run should not then claim
        to have been billed zero as if a row existed."""
        meter = FakeMeter(entry=None)
        billed = await self.orchestrator(meter)._charge(
            Run.start("x", "G" + "A" * 55), Usage(tokens=5, calls=1)
        )
        assert billed.charged == 0
