"""The run API: one idea in, one project out, with the decisions made here.

ON THE PREFIX. The proxy is a catch-all at ``/api/{service}/{path}``, so a route registered
under ``/api`` after it is unreachable — ``/api/runs`` would resolve to "forward this to a
service called `runs`". These live at ``/runs`` and the router is included BEFORE the proxy,
which makes the ordering explicit rather than incidental.

ON THE SHAPE. Every route returns the whole run, not a fragment of it. The screen then has one
thing to render and one place to read the state from, instead of stitching together what it
remembers with what the last response happened to contain — which is how it came to be the
component that knew whether a plan was approved.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from gateway.api.payment_gate import PayerDep
from gateway.application.orchestration import RunOrchestrator
from gateway.bootstrap import Container
from gateway.domain.runs import MAX_IDEA_CHARS, Answer

router = APIRouter(prefix="/runs", tags=["runs"])


def get_orchestrator(request: Request) -> RunOrchestrator:
    container: Container = request.app.state.container
    if container.orchestrator is None:  # pragma: no cover - guarded by route registration
        raise RuntimeError("orchestration is not enabled on this gateway")
    return container.orchestrator


OrchestratorDep = Annotated[RunOrchestrator, Depends(get_orchestrator)]


class StartBody(BaseModel):
    # The cap is enforced by the domain as well. Here it buys a 422 with the field named,
    # which is a better answer than a 400 with a sentence, and there it holds for callers
    # that never come through this route.
    idea: str = Field(min_length=1, max_length=MAX_IDEA_CHARS)


class AnswerBody(BaseModel):
    questionId: str  # noqa: N815 — the wire format is the screen's, and it is camelCase
    value: str


class AnswersBody(BaseModel):
    answers: list[AnswerBody] = Field(default_factory=list)


class RejectBody(BaseModel):
    feedback: str = Field(min_length=1, max_length=2_000)


@router.post("", summary="Start a run from an idea")
async def start(body: StartBody, orchestrator: OrchestratorDep, payer: PayerDep) -> dict[str, Any]:
    """The one route that decides who pays, because it is the one that starts spending.

    ``payer`` is ``""`` on a gateway that is not charging, and everything below behaves as it
    always did. When it is charging, this dependency has already refused with a 402 — and a
    price — if the caller has neither a session with a balance nor a payment.

    Nothing further down asks again: the account is written onto the run at this moment and
    read from there. Taking it from whoever later asks for the generation would let one
    signed-in caller spend another's balance by naming their run id.
    """
    run = await orchestrator.start(body.idea, payer)
    return run.as_json()


@router.get("/{run_id}", summary="Read a run")
async def read(run_id: str, orchestrator: OrchestratorDep) -> dict[str, Any]:
    """What a reloaded tab asks for.

    Before this, a run lived in the page: a refresh mid-generation lost it, and there was
    nothing to come back to.
    """
    run = await orchestrator.read(run_id)
    return run.as_json()


@router.post("/{run_id}/answers", summary="Answer the current questionnaire")
async def answer(run_id: str, body: AnswersBody, orchestrator: OrchestratorDep) -> dict[str, Any]:
    run = await orchestrator.answer(
        run_id, [Answer(question_id=a.questionId, value=a.value) for a in body.answers]
    )
    return run.as_json()


@router.post("/{run_id}/rejection", summary="Reject the plan and get the next version")
async def reject(run_id: str, body: RejectBody, orchestrator: OrchestratorDep) -> dict[str, Any]:
    run = await orchestrator.reject(run_id, body.feedback)
    return run.as_json()


@router.post("/{run_id}/approval", summary="Approve the plan")
async def approve(
    run_id: str, orchestrator: OrchestratorDep, _: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """The gate, and it takes no body ON PURPOSE.

    There is nothing for the caller to say here. The old design had the browser set
    `plan.status = "approved"` and post the plan back, so the server's check read a field the
    caller controlled — approval was a claim rather than an act. Now the act is the request
    itself, and what it means is decided by the run's state.
    """
    run = await orchestrator.approve(run_id)
    return run.as_json()


@router.get("/{run_id}/events", summary="Replay a run's stream and keep following it")
async def events(run_id: str, orchestrator: OrchestratorDep) -> StreamingResponse:
    """What a tab that lost the connection asks for.

    `GET /runs/{id}` says WHERE a run is; this says how it got there and then keeps going. It
    is a GET on purpose — it starts nothing, changes nothing, and two tabs can follow the same
    run without consuming each other's events.

    A finished run replays and ends immediately, which is what a reload after the fact wants.
    """
    # The lookup happens HERE, not inside the generator. An async generator's body does not
    # run until its first `__anext__`, which `StreamingResponse` reaches only after the
    # response has started — so a `RunNotFoundError` raised in there arrives too late to be a
    # 404 and surfaces as "response already started". `generate` below dodges the same edge
    # by pulling its first chunk early; this one only needs to ask.
    await orchestrator.read(run_id)
    return StreamingResponse(
        orchestrator.events(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/generation", summary="Generate the project")
async def generate(run_id: str, orchestrator: OrchestratorDep) -> StreamingResponse:
    """The agent's own event stream, forwarded as it arrives.

    The first transition happens before the response starts, so an unapproved run is refused
    with a 409 and a JSON body rather than with an error event inside a 200 stream that the
    screen would have to learn to read.
    """
    events = orchestrator.generate(run_id)
    first = await anext(events, b"")
    return StreamingResponse(
        _chain(first, events),
        media_type="text/event-stream",
        headers={
            # no-transform matters as much as no-cache: without it an intermediary is free to
            # compress the stream, and compressing means buffering it first.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


class ConsoleBody(BaseModel):
    # 500 is the agent's own ceiling. Here it buys a 422 with the field named instead of a
    # message out of the agent for something this route could have refused itself.
    command: str = Field(min_length=1, max_length=500)


@router.post("/{run_id}/console", summary="Run a command in the generated project")
async def console(
    run_id: str, body: ConsoleBody, orchestrator: OrchestratorDep
) -> StreamingResponse:
    """What the agent prints while the command runs, as it prints it.

    The run names the project; the caller never does. Closing the request stops the command,
    which is how the screen's "detener" works: it hangs up, the gateway closes the agent's
    connection, and the agent kills the process tree.
    """
    stream = await orchestrator.open_console(run_id, body.command)
    return StreamingResponse(
        _relay(stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{run_id}/download", summary="The generated project as a ZIP")
async def download(run_id: str, orchestrator: OrchestratorDep) -> StreamingResponse:
    """The file, with the agent's own name for it and its checksum."""
    stream = await orchestrator.open_download(run_id)
    upstream = {name.lower(): value for name, value in stream.headers}
    kept = ("content-disposition", "content-length", "x-mirag-sha256")
    return StreamingResponse(
        _relay(stream),
        media_type=upstream.get("content-type", "application/zip"),
        headers={name: upstream[name] for name in kept if name in upstream},
    )


async def _relay(stream: Any) -> Any:
    """Forward chunks as they arrive and let go of the upstream connection however this ends.

    The `finally` is the whole point for the console: when the browser hangs up, this generator
    is cancelled, and closing the stream here is what tells the agent — which is what stops
    the process.
    """
    try:
        async for chunk in stream.chunks:
            yield chunk
    finally:
        await stream.aclose()


async def _chain(first: bytes, rest: Any) -> Any:
    if first:
        yield first
    async for chunk in rest:
        yield chunk


__all__ = ["JSONResponse", "router"]
