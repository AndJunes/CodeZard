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
async def start(body: StartBody, orchestrator: OrchestratorDep) -> dict[str, Any]:
    run = await orchestrator.start(body.idea)
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
        run_id, [Answer(question_id=a.questionId, value=a.value) for a in body.answers])
    return run.as_json()


@router.post("/{run_id}/rejection", summary="Reject the plan and get the next version")
async def reject(run_id: str, body: RejectBody, orchestrator: OrchestratorDep) -> dict[str, Any]:
    run = await orchestrator.reject(run_id, body.feedback)
    return run.as_json()


@router.post("/{run_id}/approval", summary="Approve the plan")
async def approve(run_id: str, orchestrator: OrchestratorDep,
                  _: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
    """The gate, and it takes no body ON PURPOSE.

    There is nothing for the caller to say here. The old design had the browser set
    `plan.status = "approved"` and post the plan back, so the server's check read a field the
    caller controlled — approval was a claim rather than an act. Now the act is the request
    itself, and what it means is decided by the run's state.
    """
    run = await orchestrator.approve(run_id)
    return run.as_json()


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


async def _chain(first: bytes, rest: Any) -> Any:
    if first:
        yield first
    async for chunk in rest:
        yield chunk


__all__ = ["JSONResponse", "router"]
