"""The run API over HTTP, with both agents faked at the socket.

The point of these is the negative space. The old design checked approval by reading a field
out of the body the caller sent, so `curl` with `{"status":"approved"}` walked straight through
a gate the screen was drawing. Every test here that expects a 409 is a test of something that
used to be a 200.
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gateway.api.app import create_app
from gateway.config.settings import RetrySettings, ServiceSettings, Settings

PLAN = {
    "version": 1,
    "purpose": "A tracker for a community bike workshop.",
    "entities": [{"name": "Bike", "description": "in for repair", "fields": []}],
    "roles": [{"name": "Volunteer", "can": []}],
    "flows": [{"name": "Intake", "steps": ["receive", "log"]}],
    "constraints": [{"kind": "other", "statement": "standard library only"}],
    "openQuestions": ["the data model"],
    "status": "draft",
}
QUESTIONNAIRE = {
    "summary": "A tracker for bikes, parts and volunteers.",
    "questionnaire": {"reason": "two things are open", "questions": [
        {"id": "who", "text": "Who uses it day to day?"},
        {"id": "scale", "text": "How many bikes a week?"}]},
}
ARTIFACT = "0123456789abcdef01234567"
EVENTS = (
    b'data: {"type": "step", "name": "specification", "status": "executed"}\n\n'
    b'data: {"type": "step", "name": "artifact", "status": "executed"}\n\n'
    b'data: {"type": "done", "project": {"id": "' + ARTIFACT.encode() + b'"}}\n\n'
)


class Agents:
    """Both agents, answering at the transport. Records every request for inspection."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.plan_asks_again = False
        self.rounds = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/chat"):
            return httpx.Response(200, content=EVENTS,
                                  headers={"content-type": "text/event-stream"})
        if path.endswith("/analyze"):
            return httpx.Response(200, json=QUESTIONNAIRE)
        if path.endswith("/plan") and self.plan_asks_again:
            self.rounds += 1
            return httpx.Response(200, json={"reason": "one more", "questions": [
                {"id": f"q{self.rounds}", "text": "and this?"}]})
        if path.endswith("/plan"):
            return httpx.Response(200, json=PLAN)
        return httpx.Response(200, json={**PLAN, "version": 2})

    def bodies(self, suffix: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture
def agents() -> Agents:
    return Agents()


@pytest.fixture
def run_settings() -> Settings:
    return Settings(
        _env_file=None,
        services=[
            ServiceSettings(name="pm", base_url="http://pm.internal"),
            ServiceSettings(name="backend", base_url="http://backend.internal"),
        ],
        orchestration={"enabled": True, "pm_token": "pm-secret",
                       "backend_token": "backend-secret"},
        retry=RetrySettings(max_attempts=1, base_delay_seconds=0),
    )


@pytest.fixture
def run_app(run_settings: Settings, agents: Agents) -> FastAPI:
    return create_app(
        run_settings,
        http_client_factory=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(agents)),
    )


@pytest.fixture
async def runs(run_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        run_app.router.lifespan_context(run_app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=run_app),
                          base_url="http://gateway.test") as client,
    ):
        yield client


async def start(runs: httpx.AsyncClient, idea: str = "a bike workshop tracker") -> dict:
    response = await runs.post("/runs", json={"idea": idea})
    assert response.status_code == 200, response.text
    return response.json()


async def reach_plan(runs: httpx.AsyncClient) -> dict:
    run = await start(runs)
    answered = await runs.post(f"/runs/{run['runId']}/answers",
                               json={"answers": [{"questionId": "who", "value": "volunteers"}]})
    return answered.json()


class TestTheGate:
    async def test_generation_is_refused_before_a_plan_exists(
        self, runs: httpx.AsyncClient
    ) -> None:
        run = await start(runs)
        response = await runs.post(f"/runs/{run['runId']}/generation")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "illegal_transition"

    async def test_generation_is_refused_while_the_plan_is_unapproved(
        self, runs: httpx.AsyncClient
    ) -> None:
        run = await reach_plan(runs)
        assert run["state"] == "PLAN_REVIEW"
        assert (await runs.post(f"/runs/{run['runId']}/generation")).status_code == 409

    async def test_a_status_field_in_the_body_does_not_open_it(
        self, runs: httpx.AsyncClient
    ) -> None:
        """The exact bypass the old design had: claim approval in the request."""
        run = await reach_plan(runs)
        response = await runs.post(f"/runs/{run['runId']}/generation",
                                   json={"plan": {**PLAN, "status": "approved"}})
        assert response.status_code == 409

    async def test_approval_then_generation_streams(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        run = await reach_plan(runs)
        approved = await runs.post(f"/runs/{run['runId']}/approval")
        assert approved.json()["state"] == "PLAN_APPROVED"

        async with runs.stream("POST", f"/runs/{run['runId']}/generation") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert b'"name": "artifact"' in body

        after = (await runs.get(f"/runs/{run['runId']}")).json()
        assert after["state"] == "ZIP_READY"
        assert after["artifactId"] == ARTIFACT


class TestRounds:
    async def test_the_server_refuses_a_round_past_the_cap(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        agents.plan_asks_again = True
        run = await start(runs)
        statuses = []
        for i in range(4):
            response = await runs.post(f"/runs/{run['runId']}/answers",
                                       json={"answers": [{"questionId": f"q{i}", "value": "y"}]})
            statuses.append(response.status_code)
            if response.status_code != 200:
                break
        assert 502 in statuses, statuses

    async def test_the_round_number_reaches_the_agent(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        """It was sent by the screen and dropped by a route that destructured it away."""
        await reach_plan(runs)
        body = agents.bodies("/plan")[0]
        assert body["round"] == 1
        assert body["maxRounds"] == 2


class TestThePrompt:
    async def test_the_backend_is_sent_roles_and_open_questions(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        run = await reach_plan(runs)
        await runs.post(f"/runs/{run['runId']}/approval")
        async with runs.stream("POST", f"/runs/{run['runId']}/generation") as response:
            [chunk async for chunk in response.aiter_bytes()]
        question = agents.bodies("/chat")[0]["question"]
        assert "Roles: Volunteer" in question
        assert "deja abiertas" in question

    async def test_an_entity_with_no_fields_is_not_rendered_with_empty_brackets(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        run = await reach_plan(runs)
        await runs.post(f"/runs/{run['runId']}/approval")
        async with runs.stream("POST", f"/runs/{run['runId']}/generation") as response:
            [chunk async for chunk in response.aiter_bytes()]
        question = agents.bodies("/chat")[0]["question"]
        assert "()" not in question
        assert "Bike (in for repair)" in question


class TestTokens:
    async def test_each_agent_gets_its_own_token(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        """Having two is what lets the isolation be tested rather than asserted."""
        run = await reach_plan(runs)
        await runs.post(f"/runs/{run['runId']}/approval")
        async with runs.stream("POST", f"/runs/{run['runId']}/generation") as response:
            [chunk async for chunk in response.aiter_bytes()]
        by_host = {r.url.host: r.headers.get("x-mirag-token") for r in agents.requests}
        assert by_host["pm.internal"] == "pm-secret"
        assert by_host["backend.internal"] == "backend-secret"


class TestReplay:
    async def test_a_finished_run_replays_its_whole_stream(
        self, runs: httpx.AsyncClient
    ) -> None:
        """The reason reconnection is possible at all: the gateway kept what it forwarded."""
        run = await reach_plan(runs)
        await runs.post(f"/runs/{run['runId']}/approval")
        async with runs.stream("POST", f"/runs/{run['runId']}/generation") as response:
            first = b"".join([chunk async for chunk in response.aiter_bytes()])

        async with runs.stream("GET", f"/runs/{run['runId']}/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            replayed = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert replayed == first

    async def test_replaying_twice_gives_the_same_thing(
        self, runs: httpx.AsyncClient
    ) -> None:
        """A GET that starts nothing and changes nothing, so two tabs can both watch."""
        run = await reach_plan(runs)
        await runs.post(f"/runs/{run['runId']}/approval")
        async with runs.stream("POST", f"/runs/{run['runId']}/generation") as response:
            [chunk async for chunk in response.aiter_bytes()]

        seen = []
        for _ in range(2):
            async with runs.stream("GET", f"/runs/{run['runId']}/events") as response:
                seen.append(b"".join([chunk async for chunk in response.aiter_bytes()]))
        assert seen[0] == seen[1]
        assert b'"name": "artifact"' in seen[0]

    async def test_a_run_that_never_generated_has_an_empty_stream(
        self, runs: httpx.AsyncClient
    ) -> None:
        """Empty, not an error: the run exists, it simply has not said anything yet."""
        run = await start(runs)
        async with runs.stream("GET", f"/runs/{run['runId']}/events") as response:
            assert response.status_code == 200
            assert b"".join([chunk async for chunk in response.aiter_bytes()]) == b""

    async def test_an_unknown_run_is_a_404_before_anything_is_streamed(
        self, runs: httpx.AsyncClient
    ) -> None:
        response = await runs.get("/runs/0000000000000000/events")
        assert response.status_code == 404


class TestReconnecting:
    async def test_a_run_survives_the_request_that_made_it(
        self, runs: httpx.AsyncClient
    ) -> None:
        """The reason the state moved here at all: a reload used to lose everything."""
        run = await reach_plan(runs)
        again = await runs.get(f"/runs/{run['runId']}")
        assert again.status_code == 200
        assert again.json()["plan"]["purpose"] == PLAN["purpose"]

    async def test_an_unknown_run_is_a_404(self, runs: httpx.AsyncClient) -> None:
        response = await runs.get("/runs/0000000000000000")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "run_not_found"


class TestValidation:
    async def test_an_over_long_idea_is_refused_by_the_server(
        self, runs: httpx.AsyncClient
    ) -> None:
        response = await runs.post("/runs", json={"idea": "x" * 4001})
        assert response.status_code == 422

    async def test_a_rejection_needs_a_reason(self, runs: httpx.AsyncClient) -> None:
        run = await reach_plan(runs)
        response = await runs.post(f"/runs/{run['runId']}/rejection", json={"feedback": ""})
        assert response.status_code == 422


class TestRegistration:
    async def test_the_routes_are_absent_when_orchestration_is_off(self) -> None:
        """A gateway with no agents behind it should not advertise a flow it cannot run."""
        app = create_app(Settings(_env_file=None),
                         http_client_factory=lambda _: httpx.AsyncClient())
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                              base_url="http://gateway.test") as client,
        ):
            assert (await client.post("/runs", json={"idea": "x"})).status_code == 404

    async def test_the_run_routes_are_not_swallowed_by_the_proxy_catch_all(
        self, runs: httpx.AsyncClient, agents: Agents
    ) -> None:
        """`/runs` is outside `/api` on purpose, and included first as well."""
        await start(runs)
        assert not any(r.url.host == "runs" for r in agents.requests)
