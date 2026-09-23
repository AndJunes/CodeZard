"""The two routes that act on a run's generated project: the console and the ZIP.

Both go through the RUN. The browser names a run and never an artifact, and the agent's token
lives here — so the negative space is what matters: another artifact id in the request changes
nothing, and a switch that is off is a route that is not there.
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gateway.api.app import create_app
from gateway.config.settings import RetrySettings, ServiceSettings, Settings
from tests.integration.test_runs_api import ARTIFACT, Agents, reach_plan

ZIP = b"PK\x03\x04 not really a zip, only bytes that must arrive intact"
EXEC_EVENTS = (
    b'data: {"type": "start", "command": "node hello.js"}\n\n'
    b'data: {"type": "stdout", "text": "hi"}\n\n'
    b'data: {"type": "exit", "code": 0, "ms": 12, "reason": ""}\n\n'
)


class ProjectAgents(Agents):
    """The base agents plus the two routes under test, with switches to make them refuse."""

    def __init__(self) -> None:
        super().__init__()
        self.exec_status = 200
        self.download_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/exec"):
            self.requests.append(request)
            if self.exec_status == 410:
                return httpx.Response(410, json={"error": {"code": "gone", "message": "gone"}})
            if self.exec_status == 403:
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "code": "console_disabled",
                            "message": "the console is switched off on this agent",
                        }
                    },
                )
            return httpx.Response(
                200, content=EXEC_EVENTS, headers={"content-type": "text/event-stream"}
            )
        if path.endswith("/download"):
            self.requests.append(request)
            if self.download_status == 410:
                return httpx.Response(410, json={"error": {"code": "gone", "message": "gone"}})
            return httpx.Response(
                200,
                content=ZIP,
                headers={
                    "content-type": "application/zip",
                    "content-disposition": 'attachment; filename="demo.zip"',
                    "x-mirag-sha256": "abc123",
                },
            )
        return super().__call__(request)


def make_settings(console: bool) -> Settings:
    return Settings(
        _env_file=None,
        services=[
            ServiceSettings(name="pm", base_url="http://pm.internal"),
            ServiceSettings(name="backend", base_url="http://backend.internal"),
        ],
        orchestration={
            "enabled": True,
            "pm_token": "pm-secret",
            "backend_token": "backend-secret",
            "console": console,
        },
        retry=RetrySettings(max_attempts=1, base_delay_seconds=0),
    )


@pytest.fixture
def agents() -> ProjectAgents:
    return ProjectAgents()


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client,
    ):
        yield client


@pytest.fixture
async def with_console(agents: ProjectAgents) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        make_settings(True),
        http_client_factory=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(agents)),
    )
    async for client in _client(app):
        yield client


@pytest.fixture
async def without_console(agents: ProjectAgents) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        make_settings(False),
        http_client_factory=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(agents)),
    )
    async for client in _client(app):
        yield client


async def delivered(client: httpx.AsyncClient) -> str:
    """A run taken all the way to ZIP_READY, so it has an artifact."""
    run = await reach_plan(client)
    await client.post(f"/runs/{run['runId']}/approval")
    async with client.stream("POST", f"/runs/{run['runId']}/generation") as response:
        [_ async for _ in response.aiter_bytes()]
    return run["runId"]


class TestTheConsole:
    async def test_it_is_off_unless_switched_on(self, without_console: httpx.AsyncClient) -> None:
        run_id = await delivered(without_console)
        response = await without_console.post(
            f"/runs/{run_id}/console", json={"command": "node -v"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "console_disabled"

    async def test_a_run_with_no_project_has_nothing_to_run(
        self, with_console: httpx.AsyncClient
    ) -> None:
        run = await reach_plan(with_console)
        response = await with_console.post(
            f"/runs/{run['runId']}/console", json={"command": "node -v"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "no_project"

    async def test_an_unknown_run_is_a_404(self, with_console: httpx.AsyncClient) -> None:
        response = await with_console.post(f"/runs/{'0' * 32}/console", json={"command": "node -v"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "run_not_found"

    async def test_it_streams_what_the_agent_prints(
        self, with_console: httpx.AsyncClient, agents: ProjectAgents
    ) -> None:
        run_id = await delivered(with_console)
        async with with_console.stream(
            "POST", f"/runs/{run_id}/console", json={"command": "node hello.js"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert body == EXEC_EVENTS

        sent = next(r for r in agents.requests if r.url.path.endswith("/exec"))
        assert sent.url.path == f"/api/v1/artifacts/{ARTIFACT}/exec"  # from the RUN, not the caller
        assert sent.headers["x-mirag-token"] == "backend-secret"  # the token stays here
        assert json.loads(sent.content) == {"command": "node hello.js"}

    async def test_the_caller_cannot_choose_the_artifact(
        self, with_console: httpx.AsyncClient, agents: ProjectAgents
    ) -> None:
        run_id = await delivered(with_console)
        async with with_console.stream(
            "POST",
            f"/runs/{run_id}/console",
            json={
                "command": "node -v",
                "artifactId": "f" * 24,
                "artifact_id": "f" * 24,
            },
        ) as response:
            [_ async for _ in response.aiter_bytes()]
        sent = next(r for r in agents.requests if r.url.path.endswith("/exec"))
        assert "f" * 24 not in sent.url.path
        assert "f" * 24 not in sent.content.decode()

    async def test_a_forgotten_project_is_a_410_of_its_own(
        self, with_console: httpx.AsyncClient, agents: ProjectAgents
    ) -> None:
        run_id = await delivered(with_console)
        agents.exec_status = 410
        response = await with_console.post(f"/runs/{run_id}/console", json={"command": "node -v"})
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "project_gone"

    async def test_the_agents_own_refusal_is_carried_through(
        self, with_console: httpx.AsyncClient, agents: ProjectAgents
    ) -> None:
        run_id = await delivered(with_console)
        agents.exec_status = 403
        response = await with_console.post(f"/runs/{run_id}/console", json={"command": "node -v"})
        assert response.status_code == 502
        assert "switched off on this agent" in response.json()["error"]["message"]

    @pytest.mark.parametrize("body", [{}, {"command": ""}, {"command": "x" * 501}])
    async def test_a_bad_command_is_refused_here(
        self, with_console: httpx.AsyncClient, body: dict
    ) -> None:
        run_id = await delivered(with_console)
        response = await with_console.post(f"/runs/{run_id}/console", json=body)
        assert response.status_code == 422


class TestTheDownload:
    async def test_the_zip_arrives_intact_with_its_name(
        self, with_console: httpx.AsyncClient, agents: ProjectAgents
    ) -> None:
        run_id = await delivered(with_console)
        response = await with_console.get(f"/runs/{run_id}/download")
        assert response.status_code == 200
        assert response.content == ZIP
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"] == 'attachment; filename="demo.zip"'
        assert response.headers["x-mirag-sha256"] == "abc123"

        sent = next(r for r in agents.requests if r.url.path.endswith("/download"))
        assert sent.url.path == f"/api/v1/artifacts/{ARTIFACT}/download"
        assert sent.headers["x-mirag-token"] == "backend-secret"

    async def test_it_does_not_depend_on_the_console_switch(
        self, without_console: httpx.AsyncClient
    ) -> None:
        run_id = await delivered(without_console)
        assert (await without_console.get(f"/runs/{run_id}/download")).status_code == 200

    async def test_a_run_with_no_project_has_no_zip(self, with_console: httpx.AsyncClient) -> None:
        run = await reach_plan(with_console)
        response = await with_console.get(f"/runs/{run['runId']}/download")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "no_project"

    async def test_a_forgotten_project_is_a_410(
        self, with_console: httpx.AsyncClient, agents: ProjectAgents
    ) -> None:
        run_id = await delivered(with_console)
        agents.download_status = 410
        response = await with_console.get(f"/runs/{run_id}/download")
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "project_gone"
