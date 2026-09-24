"""A run: one idea, carried from the first sentence to a downloadable project.

WHY THIS IS IN THE GATEWAY AND NOT IN THE BROWSER

The rule the whole flow exists to enforce is that the backend agent does not start until a
person has approved a plan. Until now that rule lived in the browser, and the server checked
it by reading ``status`` out of the body the browser sent:

    if (plan.status !== "approved") return 409

Which is not a check. Delete the field and the gate opens; set it by hand and the gate opens.
The state machine that decides which move is legal shipped in the page bundle, with a comment
saying it was there *"so the server can refuse a request that never went through the screen"* —
a thing the server had no way to do, because it remembered nothing between requests.

A run is what the server remembers. Approval becomes a transition it performed, not a field it
was handed, and the round cap becomes a number it owns rather than a number it is told.

WHAT A RUN IS NOT

It is not persistence. TC-5 and DR-4 of the PRD forbid carrying anything between executions,
and nothing here touches a disk: a run lives in the process, expires, and is gone. The
precedent is already in this codebase — ``CircuitBreakerUpstreamClient`` keeps per-service
state in a dictionary for exactly as long as the process lives, for the same reason.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from gateway.domain.exceptions import GatewayError

MAX_ROUNDS = 2
"""Questionnaires a run may serve before the plan has to be written.

It was in the browser as a constant, sent to the agent on every call, and dropped on the way
by a server route that destructured `{ idea, answers }` and threw the rest away. So the only
thing preventing a third round was the model not asking for one. Here it is counted by
whoever grants the rounds."""

MAX_IDEA_CHARS = 4_000
"""FR-1.1. It was enforced by a `maxlength` attribute, which is a courtesy to whoever is
typing and nothing at all to whoever is not."""

RUN_TTL_SECONDS = 60 * 60
"""How long an abandoned run stays readable. Long enough to survive a reload and a slow
generation; short enough that a browser closed at lunchtime is gone by the afternoon."""


class RunState(StrEnum):
    """Where a run is. The names are the ones the screen already used."""

    IDEA = "IDEA"
    PM_ANALYSIS = "PM_ANALYSIS"
    QUESTIONNAIRE = "QUESTIONNAIRE"
    PLAN_REVIEW = "PLAN_REVIEW"
    PLAN_REJECTED = "PLAN_REJECTED"
    PM_REVISION = "PM_REVISION"
    PLAN_APPROVED = "PLAN_APPROVED"
    BACKEND_GENERATION = "BACKEND_GENERATION"
    ZIP_READY = "ZIP_READY"
    FAILED = "FAILED"
    """New here. The browser's table had no failure state, so a run that died mid-generation
    simply stopped moving, and nothing could tell that from one still thinking."""


class RunEvent(StrEnum):
    DESCRIBE = "DESCRIBE"
    ASK = "ASK"
    PROPOSE = "PROPOSE"
    REJECT = "REJECT"
    REVISE = "REVISE"
    APPROVE = "APPROVE"
    GENERATE = "GENERATE"
    DELIVER = "DELIVER"
    FAIL = "FAIL"
    RESET = "RESET"


TRANSITIONS: Mapping[RunState, Mapping[RunEvent, RunState]] = {
    RunState.IDEA: {RunEvent.DESCRIBE: RunState.PM_ANALYSIS},
    # The PM may ask again after reading the answers: a second round is a normal outcome, not
    # a failure. That is why ASK loops back.
    RunState.PM_ANALYSIS: {
        RunEvent.ASK: RunState.QUESTIONNAIRE,
        RunEvent.PROPOSE: RunState.PLAN_REVIEW,
    },
    RunState.QUESTIONNAIRE: {
        RunEvent.DESCRIBE: RunState.PM_ANALYSIS,
        RunEvent.ASK: RunState.QUESTIONNAIRE,
        RunEvent.PROPOSE: RunState.PLAN_REVIEW,
    },
    RunState.PLAN_REVIEW: {
        RunEvent.REJECT: RunState.PLAN_REJECTED,
        RunEvent.APPROVE: RunState.PLAN_APPROVED,
    },
    RunState.PLAN_REJECTED: {RunEvent.REVISE: RunState.PM_REVISION},
    RunState.PM_REVISION: {RunEvent.PROPOSE: RunState.PLAN_REVIEW},
    # No way back. Once approved the plan is frozen for this run: CA-08.
    RunState.PLAN_APPROVED: {RunEvent.GENERATE: RunState.BACKEND_GENERATION},
    RunState.BACKEND_GENERATION: {RunEvent.DELIVER: RunState.ZIP_READY},
    RunState.ZIP_READY: {RunEvent.RESET: RunState.IDEA},
    RunState.FAILED: {RunEvent.RESET: RunState.IDEA},
}

FAILABLE = frozenset(TRANSITIONS) - {RunState.ZIP_READY, RunState.FAILED}
"""Anything in flight can fail. Spelling FAIL into every row of the table would say the same
thing nine times and leave the ninth out when a state is added."""


class IllegalTransitionError(GatewayError):
    """A move the run's state does not allow.

    It carries both ends because "409" alone sends whoever reads the log back to the source
    to find out which move was refused and from where.
    """

    def __init__(self, state: RunState, event: RunEvent) -> None:
        super().__init__(f"{event} is not allowed from {state}")
        self.state = state
        self.event = event


@dataclass(frozen=True, slots=True)
class Answer:
    question_id: str
    value: str


@dataclass(frozen=True, slots=True)
class Run:
    """Immutable. Every move returns a new one, the way ``Project`` does in the agent.

    The store replaces the old value, so a run that fails a transition leaves nothing
    half-changed behind.
    """

    id: str
    idea: str
    state: RunState = RunState.IDEA
    rounds: int = 0
    """Questionnaires SERVED. Compared against MAX_ROUNDS by the server that serves them."""
    answers: tuple[Answer, ...] = ()
    plan: Mapping[str, Any] | None = None
    summary: str = ""
    questionnaire: Mapping[str, Any] | None = None
    artifact_id: str = ""
    error: str = ""
    account: str = ""
    """Who pays for this run, when the gateway is charging. Empty on a gateway that is not.

    Recorded at the start and never read from a later request. The alternative — taking the
    account from whoever asks for the generation — would let one signed-in caller spend
    another's balance by naming their run id, and a run id is not a secret strong enough to
    be the only thing between two accounts' money."""
    charged: int = 0
    """Tokens debited for this run, once it finished. Zero until then, and zero forever on a
    run that never delivered: a generation that failed after the model was paid is our loss."""
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)

    @classmethod
    def start(cls, idea: str, account: str = "") -> Run:
        text = (idea or "").strip()
        if not text:
            raise ValueError("a run needs an idea")
        if len(text) > MAX_IDEA_CHARS:
            raise ValueError(f"the idea is longer than {MAX_IDEA_CHARS} characters")
        # 16 bytes of urandom, hex: long enough that a run id is not worth guessing at, and
        # guessing is the only way to reach someone else's when nobody is signed in.
        return cls(id=secrets.token_hex(16), idea=text, account=account)

    def billed(self, tokens: int) -> Run:
        """What the run cost, recorded on it. Not a transition: the state is already final."""
        return replace(self, charged=tokens, updated_at=time.monotonic())

    # ── moving ───────────────────────────────────────────────────────────────

    def may(self, event: RunEvent) -> bool:
        if event is RunEvent.FAIL:
            return self.state in FAILABLE
        return event in TRANSITIONS.get(self.state, {})

    def _moved(self, event: RunEvent, **changes: Any) -> Run:
        if not self.may(event):
            raise IllegalTransitionError(self.state, event)
        state = RunState.FAILED if event is RunEvent.FAIL else TRANSITIONS[self.state][event]
        return replace(self, state=state, updated_at=time.monotonic(), **changes)

    def describing(self) -> Run:
        return self._moved(RunEvent.DESCRIBE)

    def asked(self, summary: str, questionnaire: Mapping[str, Any]) -> Run:
        """A questionnaire was served. This is the ONLY place `rounds` grows."""
        return self._moved(
            RunEvent.ASK,
            summary=summary or self.summary,
            questionnaire=questionnaire,
            rounds=self.rounds + 1,
        )

    def answering(self, answers: Sequence[Answer]) -> Run:
        """Answers are MERGED by question id, never replaced.

        The browser replaced them each round, so the second round re-decided with less than
        the first had. Merging is the behaviour; it lives here now so it cannot be lost by
        whichever caller forgets.
        """
        merged = {answer.question_id: answer for answer in self.answers}
        merged.update({answer.question_id: answer for answer in answers})
        return replace(self, answers=tuple(merged.values()), updated_at=time.monotonic())

    def proposed(self, plan: Mapping[str, Any]) -> Run:
        return self._moved(RunEvent.PROPOSE, plan=plan, questionnaire=None)

    def rejected(self) -> Run:
        return self._moved(RunEvent.REJECT)

    def revising(self) -> Run:
        return self._moved(RunEvent.REVISE)

    def approved(self) -> Run:
        """The transition that used to be an assignment in the browser."""
        return self._moved(RunEvent.APPROVE)

    def generating(self) -> Run:
        return self._moved(RunEvent.GENERATE)

    def delivered(self, artifact_id: str) -> Run:
        return self._moved(RunEvent.DELIVER, artifact_id=artifact_id)

    def failed(self, error: str) -> Run:
        return self._moved(RunEvent.FAIL, error=error)

    # ── what the screen is allowed to know ───────────────────────────────────

    @property
    def rounds_left(self) -> int:
        return max(0, MAX_ROUNDS - self.rounds)

    @property
    def may_generate(self) -> bool:
        """The single gate. Read from the run's own state, never from a request body."""
        return self.may(RunEvent.GENERATE)

    def as_json(self) -> dict[str, Any]:
        return {
            "runId": self.id,
            "state": str(self.state),
            "idea": self.idea,
            "summary": self.summary,
            "questionnaire": self.questionnaire,
            "plan": self.plan,
            "answers": [{"questionId": a.question_id, "value": a.value} for a in self.answers],
            "round": self.rounds,
            "maxRounds": MAX_ROUNDS,
            "artifactId": self.artifact_id,
            "error": self.error,
            # The address is the account, so it is the caller's own public key coming back to
            # them — never anybody else's, because a run is only ever read by whoever holds
            # its id and, when billing is on, whoever the run belongs to.
            "account": self.account,
            "charged": self.charged,
        }
