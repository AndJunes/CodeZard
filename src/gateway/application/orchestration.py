"""Use case: carry a run from an idea to a generated project.

This is the file the browser used to be. It holds the state machine, the round cap, the
merge policy for answers, the decision of whether an agent answered with a plan or with more
questions, and the prompt the backend agent is given — every one of which shipped in the page
bundle, where anyone could edit it and nobody could enforce it.

The gateway needed no new routing to do this: `ProxyService` already resolves a name to a
service and speaks both `send` and `stream`. What it needed was something that decides, and
this is that. It calls the PM with `send`, because a plan is one JSON document, and the
backend with `stream`, because a generation reports progress for minutes.

WHAT IS DELIBERATELY NOT HERE

Rendering. The events this emits carry the agent's own step names and summaries; how they are
drawn, in which order, and with what wording is the screen's business and stays there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from gateway.application.proxy_service import ProxyService
from gateway.config.settings import OrchestrationSettings
from gateway.domain.billing import Usage
from gateway.domain.exceptions import (
    ConsoleDisabledError,
    NoProjectError,
    ProjectGoneError,
    UpstreamError,
)
from gateway.domain.models import InboundRequest, UpstreamStream
from gateway.domain.ports import RunLog, RunMeter, RunStore
from gateway.domain.runs import MAX_ROUNDS, Answer, IllegalTransitionError, Run

logger = logging.getLogger(__name__)

HEADER = "Generá un proyecto completo, con su estructura de archivos, README y tests."
"""The first line, and it has to be there.

The agent decides between "one file" and "a whole project" by reading the request, and a plan
that only described entities and flows read as the first: it answered with a single snippet
and the delivery arrived with no files at all. CodeZard only ever asks for the second kind —
every run here ends in a downloadable tree — so the prompt says so instead of leaving the
agent to infer it from vocabulary that happened to be missing.
"""


def prompt_for(plan: Mapping[str, Any]) -> str:
    """The approved plan as the single question the backend agent takes.

    Empty sections are omitted rather than emitted blank: a trailing "Entidades:" with nothing
    after it is noise to a model, and it changed the text enough that the agent stopped
    recognising a question it otherwise answers.

    Two things here were being dropped silently. `roles`, because who uses the thing changes
    what gets built — a coordinator's dashboard and a volunteer's list are not the same
    screen. And `openQuestions`, which matters most: those are the decisions the plan
    deliberately did not make. Sent, the agent decides them and says what it assumed; withheld,
    it decides them anyway and nobody finds out which.
    """
    sections: list[str] = []
    if entities := _items(plan.get("entities")):
        sections.append("Entidades: " + "; ".join(_entity(e) for e in entities))
    if roles := _items(plan.get("roles")):
        sections.append("Roles: " + "; ".join(str(r.get("name") or "") for r in roles))
    if flows := _items(plan.get("flows")):
        sections.append(
            "Flujos: "
            + "; ".join(
                f"{f.get('name')}: {' → '.join(str(s) for s in _list(f.get('steps')))}"
                for f in flows
            )
        )
    if constraints := _items(plan.get("constraints")):
        sections.append(
            "Restricciones: " + "; ".join(str(c.get("statement") or "") for c in constraints)
        )
    if open_questions := [str(q) for q in _list(plan.get("openQuestions"))]:
        sections.append(
            "Decisiones que el plan deja abiertas (resolvelas y dejá dicho qué "
            "asumiste): " + "; ".join(open_questions)
        )
    purpose = str(plan.get("purpose") or "")
    return "\n".join([HEADER, "", purpose, *(["", *sections] if sections else [])])


def _entity(entity: Mapping[str, Any]) -> str:
    """`Bike (frame number, owner)`, or just `Bike`.

    The PM empties `fields` ON PURPOSE — its schema says a field list is a data schema and
    the implementer's to derive — so every real plan rendered "Bike (); Part (); Volunteer ()".
    An entity's description says far more than an empty pair of brackets.
    """
    name = str(entity.get("name") or "")
    detail = ", ".join(str(f) for f in _list(entity.get("fields")))
    detail = detail or str(entity.get("description") or "").strip()
    return f"{name} ({detail})" if detail else name


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in _list(value) if isinstance(v, Mapping)]


class OrchestrationError(UpstreamError):
    """An agent answered, and what it said cannot be used."""


class RunOrchestrator:
    """One instance, shared. It holds no per-run state: the store does."""

    def __init__(
        self,
        proxy: ProxyService,
        runs: RunStore,
        settings: OrchestrationSettings,
        log: RunLog,
        meter: RunMeter | None = None,
    ) -> None:
        self._proxy = proxy
        self._runs = runs
        self._settings = settings
        self._log = log
        self._meter = meter
        """Billing, or ``None``. A gateway that is not charging behaves exactly as before:
        every call to it is guarded, and no path here changes shape because of it."""

    # ── the moves ────────────────────────────────────────────────────────────

    async def start(self, idea: str, account: str = "") -> Run:
        """An idea becomes a run, and the PM is asked what it understood.

        Authorised BEFORE the first model call, which is the only honest moment to refuse:
        the PM analysis costs money too, and an account that cannot pay should be told so
        while there is still nothing to lose.
        """
        if self._meter is not None and account:
            await self._meter.authorize(account)
        run = Run.start(idea, account).describing()
        answer = await self._ask_pm("analyze", {"idea": run.idea})
        summary = str(answer.get("summary") or "")
        questionnaire = answer.get("questionnaire")
        if questions := _questions(questionnaire):
            reason = questionnaire.get("reason") if isinstance(questionnaire, Mapping) else ""
            run = run.asked(summary, {"reason": str(reason or ""), "questions": questions})
        else:
            # No questions is a legitimate answer — the agent only asks what it cannot infer.
            # The run goes straight to planning rather than serving an empty questionnaire,
            # which is what crashed the screen: it opened the popup and read question zero
            # of none.
            run = await self._plan(run.asked(summary, {"reason": "", "questions": []}))
        return await self._runs.put(run)

    async def answer(self, run_id: str, answers: Sequence[Answer]) -> Run:
        """The answers come back, and the PM either plans or asks once more."""
        run = await self._runs.get(run_id)
        run = await self._plan(run.answering(answers))
        return await self._runs.put(run)

    async def reject(self, run_id: str, feedback: str) -> Run:
        """A rejection. The next version is a new plan, never an edit of the last."""
        text = (feedback or "").strip()
        if not text:
            raise ValueError("a rejection needs a reason")
        run = (await self._runs.get(run_id)).rejected().revising()
        answer = await self._ask_pm("revise", {"plan": run.plan, "feedback": text})
        if not _is_plan(answer):
            raise OrchestrationError(
                self._settings.pm_service, "the PM did not return a revised plan"
            )
        return await self._runs.put(run.proposed(answer))

    async def approve(self, run_id: str) -> Run:
        """The gate. A transition the server performs, not a field it is handed.

        The browser did `plan.status = "approved"` and posted the object; both server checks
        read that field out of the body. Removing it opened the gate; writing it by hand
        opened the gate.
        """
        run = (await self._runs.get(run_id)).approved()
        return await self._runs.put(run)

    async def generate(self, run_id: str) -> AsyncIterator[bytes]:
        """The backend agent's stream, with the run's state moved around it.

        The bytes are forwarded as they arrive and never buffered: the agent reports for
        minutes, and anything that waits for the last byte turns that into a blank screen
        followed by everything at once.
        """
        run = (await self._runs.get(run_id)).generating()
        await self._runs.put(run)

        body = json.dumps(
            {"question": prompt_for(run.plan or {}), "locale": self._settings.locale}
        ).encode("utf-8")
        stream = await self._proxy.stream(
            self._settings.backend_service,
            InboundRequest(
                method="POST",
                path="api/v1/chat",
                body=body,
                headers=self._agent_headers(self._settings.backend_token),
            ),
        )

        scan = StreamScan()
        self._log.start(run.id)
        try:
            async for chunk in stream.chunks:
                # Kept BEFORE it is yielded, so a caller that disappears mid-chunk does not
                # take the event with it. This is the whole of reconnection: the browser held
                # the only copy of what it had seen, and a reload lost it while the work
                # carried on server-side with nobody able to watch.
                self._log.append(run.id, chunk)
                yield chunk
                # Read along the way rather than parse afterwards: the artifact id and what
                # the run consumed are the two things the run must keep, and by the time the
                # stream ends the caller may already be gone.
                scan.feed(chunk)
        except asyncio.CancelledError:
            # The BROWSER went away, and this is not an `Exception`: `CancelledError` derives
            # from `BaseException`, so the clause below never saw it and the run was left
            # sitting in BACKEND_GENERATION with no error, forever. A reconnecting tab then
            # asked for a run that said it was still working and never would be again.
            #
            # Measured: the front end's proxy hit undici's 300-second body timeout during a
            # long model call, dropped the stream, and left exactly that.
            logger.info("run %s: the client went away mid-generation", run.id)
            await self._runs.put(run.failed("the connection was lost during generation"))
            raise
        except Exception as error:
            logger.warning("run %s: generation failed: %s", run.id, error)
            await self._runs.put(run.failed(f"{type(error).__name__}: {error}"))
            raise
        finally:
            # This stream belongs to us the moment we chain it inside our own generator, and
            # a discarded stream still holds a connection from the pool. `retry.py` is the
            # precedent: whoever drops one closes it.
            await stream.aclose()
            # However it ended — delivered, failed, or the client walking away — the readers
            # following this log have to be let go.
            self._log.end(run.id)

        if not scan.artifact:
            await self._runs.put(run.failed("the agent produced no artifact"))
            return
        delivered = run.delivered(scan.artifact)
        await self._runs.put(await self._charge(delivered, scan.usage))

    async def _charge(self, run: Run, usage: Usage) -> Run:
        """Debit what the run consumed, once it has actually delivered something.

        Only on delivery, and that is a decision rather than an oversight. A generation that
        dies halfway has still cost us what the model charged, and billing a person for a ZIP
        they never received is the kind of thing that is technically defensible and loses the
        customer. The spend shows up in our own costs; it does not show up in theirs.

        A failure to bill never fails the run either. The project exists, the caller is
        watching it arrive, and a ledger that could not be written is our problem to notice in
        the logs — not a reason to turn a finished generation into an error.
        """
        if self._meter is None or not run.account or usage.is_empty:
            return run
        try:
            entry = await self._meter.charge(run.account, run.id, usage)
        except Exception as error:
            logger.error("run %s could not be charged: %s", run.id, error)
            return run
        return run.billed(abs(getattr(entry, "tokens", 0))) if entry is not None else run

    async def read(self, run_id: str) -> Run:
        """What a reloaded tab asks for. The whole reason the state is here."""
        return await self._runs.get(run_id)

    async def events(self, run_id: str) -> AsyncIterator[bytes]:
        """Everything this run has emitted, then everything it emits next.

        `read` says WHERE a run is; this says how it got there. A tab that reconnects mid
        generation replays what it missed and then keeps watching the same stream, rather
        than staring at a state that will not change for another ten minutes.
        """
        async for chunk in self._log.follow(run_id):
            yield chunk

    # ── the project, once it exists ──────────────────────────────────────────

    async def open_console(self, run_id: str, command: str) -> UpstreamStream:
        """Start a command in the run's project and hand back the agent's event stream.

        The browser gets NO way to name an artifact: it names a run, and the run knows which
        project is its own. An id in the request would let one tab run commands in another's
        project, and the agent's token would be the only thing between them.

        Two switches, and this is the first: the agent has its own (`MIRAG_CONSOLE`). The
        gateway does not execute anything; it only decides who may ask.
        """
        if not self._settings.console:
            raise ConsoleDisabledError()
        artifact = await self._artifact_of(run_id)
        return await self._open(
            InboundRequest(
                method="POST",
                path=f"api/v1/artifacts/{artifact}/exec",
                body=json.dumps({"command": command}).encode("utf-8"),
                headers=self._agent_headers(self._settings.backend_token),
            )
        )

    async def open_download(self, run_id: str) -> UpstreamStream:
        """The project's ZIP, streamed from the agent with the token the browser never has.

        The agent's `download_url` is relative to the AGENT and its route is protected, so the
        link the screen used to draw pointed at nothing: 404 at the page's own origin, 401 at
        the agent. Going through the run fixes both without the browser learning either.
        """
        artifact = await self._artifact_of(run_id)
        return await self._open(
            InboundRequest(
                method="GET",
                path=f"api/v1/artifacts/{artifact}/download",
                headers=self._agent_headers(self._settings.backend_token),
            )
        )

    async def _artifact_of(self, run_id: str) -> str:
        run = await self._runs.get(run_id)
        if not run.artifact_id:
            raise NoProjectError(run_id)
        return run.artifact_id

    async def _open(self, request: InboundRequest) -> UpstreamStream:
        """Open a stream and turn the agent's refusals into something a screen can say.

        Checked BEFORE handing the stream back: a 4xx read as if it were the stream would reach
        the browser as a 200 whose body is an error nobody is looking for.
        """
        stream = await self._proxy.stream(self._settings.backend_service, request)
        if stream.status_code < 400:
            return stream
        try:
            body = b"".join([chunk async for chunk in stream.chunks])[:4096]
        finally:
            await stream.aclose()
        try:
            detail = str(((json.loads(body) or {}).get("error") or {}).get("message") or "")
        except (ValueError, AttributeError):
            detail = ""
        if stream.status_code == 410:
            raise ProjectGoneError()
        raise OrchestrationError(
            self._settings.backend_service, detail or f"the agent answered {stream.status_code}"
        )

    # ── the machinery ────────────────────────────────────────────────────────

    async def _plan(self, run: Run) -> Run:
        """One planning turn: a plan, or another questionnaire if rounds remain."""
        answer = await self._ask_pm(
            "plan",
            {
                "idea": run.idea,
                "answers": [{"questionId": a.question_id, "value": a.value} for a in run.answers],
                # Told, and now true. The browser sent these and a server route destructured
                # `{ idea, answers }` and dropped them, so the agent fell back to its own default
                # and the cap was enforced by nothing.
                "round": run.rounds,
                "maxRounds": MAX_ROUNDS,
            },
        )
        if questions := _questions(answer):
            if run.rounds_left <= 0:
                # The agent is not asked to stop asking; it is not given the tool. If one
                # slips through anyway the server is the one that says no, which is the
                # difference between a policy and a hope.
                raise OrchestrationError(
                    self._settings.pm_service,
                    f"the PM asked for round {run.rounds + 1} of {MAX_ROUNDS}",
                )
            return run.asked(
                run.summary, {"reason": str(answer.get("reason") or ""), "questions": questions}
            )
        if not _is_plan(answer):
            raise OrchestrationError(
                self._settings.pm_service, "the PM returned neither a plan nor questions"
            )
        return run.proposed(answer)

    async def _ask_pm(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps({**payload, "locale": self._settings.locale}).encode("utf-8")
        response = await self._proxy.forward(
            self._settings.pm_service,
            InboundRequest(
                method="POST",
                path=f"api/v1/{operation}",
                body=body,
                headers=self._agent_headers(self._settings.pm_token),
            ),
        )
        try:
            answer = json.loads(response.body or b"{}")
        except ValueError as error:
            raise OrchestrationError(
                self._settings.pm_service, f"{operation} did not answer JSON: {error}"
            ) from error
        if response.status_code >= 400 or not isinstance(answer, dict):
            # The agent's OWN message is carried through rather than replaced. Its 503 says
            # which provider refused and why, which is the single most likely thing to go
            # wrong here and is useless as a generic "agent unavailable".
            detail = (answer.get("error") or {}).get("message") if isinstance(answer, dict) else ""
            raise OrchestrationError(
                self._settings.pm_service,
                str(detail or f"{operation} answered {response.status_code}"),
            )
        return answer

    def _agent_headers(self, token: str) -> tuple[tuple[str, str], ...]:
        headers = [("content-type", "application/json")]
        if token:
            headers.append(("x-mirag-token", token))
        return tuple(headers)


# ── reading what an agent sent ───────────────────────────────────────────────


def _questions(answer: Any) -> list[Mapping[str, Any]]:
    """The questions in an answer, or none.

    Both shapes are accepted because the PM uses both: `analyze` nests them under
    `questionnaire`, `plan` returns them at the top level.
    """
    if not isinstance(answer, Mapping):
        return []
    nested = answer.get("questionnaire")
    source = nested if isinstance(nested, Mapping) else answer
    return [
        q
        for q in _list(source.get("questions"))
        if isinstance(q, Mapping) and str(q.get("text") or "").strip()
    ]


def _is_plan(answer: Any) -> bool:
    """A plan says what it is for AND names something concrete.

    Both halves, because each has been seen without the other: an empty `deliver_plan` came
    back through the agent's API as a plan with 200, no purpose and no entities.
    """
    if not isinstance(answer, Mapping) or not str(answer.get("purpose") or "").strip():
        return False
    return any(_items(answer.get(field)) for field in ("entities", "roles", "flows", "constraints"))


ARTIFACT_ID = re.compile(rb"\b([0-9a-f]{24})\b")
"""The agent's artifact ids. Used only as the fallback below."""

MAX_TAIL = 8192
"""Cap on the partial last line kept between chunks, so a stream with no newline at all
cannot grow without bound."""


class StreamScan:
    """Reads the agent's stream as it goes past, keeping the two things the run must outlive it by.

    Reading along the way is not an optimisation. The caller can disconnect at any moment —
    closing the tab is the normal way this ends — and the run still has to know what was
    produced, so a reconnect can offer the download, and what it consumed, so it can be
    billed.

    The artifact id is taken from the `done` event's `project.id`, which is a field. The
    `artifact` step LOOKS like the obvious source and is not: its `detail` is null and the id
    appears only inside its human-readable summary, so reading it there means parsing a
    sentence written for a person. That sentence is the fallback, for a stream cut off before
    `done`.

    The usage comes from the `done` event's `cost.usage` object and from nowhere else. The
    same panel carries a `text` field — "$0.0123", "gratis", "salió de un guion" — written for
    a person and free to change wording. A billing input parsed out of a sentence is a billing
    input that will one day be parsed wrong.
    """

    __slots__ = ("_tail", "artifact", "usage")

    def __init__(self) -> None:
        self.artifact = ""
        self.usage = Usage()
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        """Take one chunk. An SSE event is routinely split across two of them."""
        buffer = self._tail + chunk
        lines = buffer.split(b"\n")
        self._tail = lines[-1][-MAX_TAIL:]
        for line in lines[:-1]:
            self._read(line)

    def _read(self, line: bytes) -> None:
        if not line.startswith(b"data: "):
            return
        try:
            event = json.loads(line[6:])
        except ValueError:
            return
        if not isinstance(event, Mapping):
            return
        if event.get("type") == "done":
            project = event.get("project")
            if isinstance(project, Mapping) and project.get("id"):
                self.artifact = str(project["id"])
            cost = event.get("cost")
            if isinstance(cost, Mapping):
                self.usage = Usage.from_agent(cost)
            return
        if (
            not self.artifact
            and event.get("name") == "artifact"
            and (match := ARTIFACT_ID.search(line))
        ):
            self.artifact = match.group(1).decode()


__all__ = [
    "IllegalTransitionError",
    "OrchestrationError",
    "RunOrchestrator",
    "StreamScan",
    "prompt_for",
]
