"""The run aggregate and its store.

These are the rules that used to live in the browser, so the tests are mostly about what is
NOT allowed. A state machine is only worth having if something refuses.
"""

import asyncio

import pytest

from gateway.domain.exceptions import RunNotFoundError
from gateway.domain.runs import (
    MAX_IDEA_CHARS,
    MAX_ROUNDS,
    Answer,
    IllegalTransitionError,
    Run,
    RunEvent,
    RunState,
)
from gateway.infrastructure.runs import InMemoryRunStore


def planned(idea: str = "a bike workshop tracker") -> Run:
    """A run parked at PLAN_REVIEW, which is where most of the interesting refusals are."""
    return Run.start(idea).describing().asked("understood", {"questions": [{"id": "a"}]}) \
        .proposed({"purpose": "x"})


class TestStarting:
    def test_an_idea_becomes_a_run(self) -> None:
        run = Run.start("  a bike workshop tracker  ")
        assert run.idea == "a bike workshop tracker"
        assert run.state is RunState.IDEA
        assert len(run.id) == 32

    def test_two_runs_do_not_share_an_id(self) -> None:
        assert Run.start("one").id != Run.start("one").id

    @pytest.mark.parametrize("idea", ["", "   ", "\n"])
    def test_an_empty_idea_is_refused(self, idea: str) -> None:
        with pytest.raises(ValueError, match="needs an idea"):
            Run.start(idea)

    def test_fr_1_1_is_enforced_here_and_not_by_a_maxlength_attribute(self) -> None:
        with pytest.raises(ValueError, match="longer than"):
            Run.start("x" * (MAX_IDEA_CHARS + 1))


class TestTheGate:
    """The one rule the whole flow exists for."""

    def test_a_fresh_run_may_not_generate(self) -> None:
        assert Run.start("x").may_generate is False

    def test_a_plan_under_review_may_not_generate(self) -> None:
        assert planned().may_generate is False

    def test_only_approval_opens_it(self) -> None:
        assert planned().approved().may_generate is True

    def test_generating_without_approval_raises(self) -> None:
        with pytest.raises(IllegalTransitionError) as raised:
            planned().generating()
        assert raised.value.event is RunEvent.GENERATE
        assert raised.value.state is RunState.PLAN_REVIEW

    def test_an_approved_plan_cannot_be_revised(self) -> None:
        """CA-08: once approved the plan is frozen for this run."""
        with pytest.raises(IllegalTransitionError):
            planned().approved().rejected()


class TestRounds:
    def test_only_serving_a_questionnaire_spends_a_round(self) -> None:
        run = Run.start("x").describing()
        assert run.rounds == 0
        assert run.asked("s", {}).rounds == 1

    def test_answering_does_not_spend_one(self) -> None:
        run = Run.start("x").describing().asked("s", {})
        assert run.answering([Answer("a", "yes")]).rounds == 1

    def test_the_cap_is_reached_after_max_rounds(self) -> None:
        run = Run.start("x").describing()
        for _ in range(MAX_ROUNDS):
            run = run.asked("s", {})
        assert run.rounds_left == 0


class TestAnswers:
    def test_answers_are_merged_by_question_id_not_replaced(self) -> None:
        """The browser replaced them each round, so round two re-decided with less than
        round one had."""
        run = Run.start("x").answering([Answer("who", "volunteers"), Answer("scale", "30")])
        run = run.answering([Answer("scale", "50"), Answer("when", "Saturdays")])
        assert {a.question_id: a.value for a in run.answers} == {
            "who": "volunteers", "scale": "50", "when": "Saturdays"}

    def test_the_order_of_first_appearance_is_kept(self) -> None:
        run = Run.start("x").answering([Answer("a", "1"), Answer("b", "2")])
        run = run.answering([Answer("a", "3")])
        assert [a.question_id for a in run.answers] == ["a", "b"]


class TestFailing:
    def test_anything_in_flight_can_fail(self) -> None:
        run = planned().approved().generating().failed("the provider refused")
        assert run.state is RunState.FAILED
        assert run.error == "the provider refused"

    def test_a_delivered_run_cannot_fail_afterwards(self) -> None:
        run = planned().approved().generating().delivered("abc")
        with pytest.raises(IllegalTransitionError):
            run.failed("too late")


class TestImmutability:
    def test_a_move_returns_a_new_run(self) -> None:
        before = Run.start("x")
        after = before.describing()
        assert before.state is RunState.IDEA
        assert after.state is RunState.PM_ANALYSIS

    def test_a_refused_move_changes_nothing(self) -> None:
        run = planned()
        with pytest.raises(IllegalTransitionError):
            run.generating()
        assert run.state is RunState.PLAN_REVIEW


class TestStore:
    async def test_a_run_is_readable_by_id(self) -> None:
        store = InMemoryRunStore()
        run = await store.put(Run.start("x"))
        assert (await store.get(run.id)).idea == "x"

    async def test_an_unknown_id_is_not_found(self) -> None:
        with pytest.raises(RunNotFoundError):
            await InMemoryRunStore().get("nope")

    async def test_an_expired_run_is_indistinguishable_from_a_missing_one(self) -> None:
        store = InMemoryRunStore(ttl_seconds=0.01)
        run = await store.put(Run.start("x"))
        await asyncio.sleep(0.05)
        with pytest.raises(RunNotFoundError):
            await store.get(run.id)

    async def test_the_store_is_bounded(self) -> None:
        store = InMemoryRunStore(max_runs=3)
        for i in range(10):
            await store.put(Run.start(f"run {i}"))
        assert len(store) == 3

    async def test_eviction_takes_the_least_recently_updated_not_inserted(self) -> None:
        """Insertion order is not update order. Without the reinsertion in ``put`` a long
        healthy run would be evicted before a short abandoned one."""
        store = InMemoryRunStore(max_runs=2)
        old = await store.put(Run.start("kept"))
        await store.put(Run.start("filler"))
        await store.put(old.describing())
        await store.put(Run.start("newcomer"))
        assert (await store.get(old.id)).idea == "kept"

    async def test_concurrent_writes_do_not_lose_runs(self) -> None:
        store = InMemoryRunStore(max_runs=100)
        runs = [Run.start(f"run {i}") for i in range(50)]
        await asyncio.gather(*(store.put(run) for run in runs))
        assert len(store) == 50
