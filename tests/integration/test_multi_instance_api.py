"""A service running on several instances, end to end: spreading, pinning and failover."""

from collections.abc import Callable

import httpx
import pytest

from gateway.config.settings import (
    CircuitBreakerSettings,
    RetrySettings,
    ServiceSettings,
    Settings,
)
from gateway.domain.models import ServiceInstance
from tests.integration.conftest import FAILURE_THRESHOLD, MAX_ATTEMPTS, UpstreamStub

HOST_A, HOST_B = "agent-a.internal", "agent-b.internal"
ID_A, ID_B = (ServiceInstance(f"http://{host}").id for host in (HOST_A, HOST_B))
TOKEN = "agent-shared-secret"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        services=[
            ServiceSettings(
                name="agent",
                base_urls=[f"http://{HOST_A}", f"http://{HOST_B}"],
                health_path="/api/v1/health",
                headers={"X-Agent-Token": TOKEN},
            )
        ],
        retry=RetrySettings(max_attempts=MAX_ATTEMPTS, base_delay_seconds=0),
        circuit_breaker=CircuitBreakerSettings(
            failure_threshold=FAILURE_THRESHOLD, recovery_timeout_seconds=60
        ),
    )


def fail_on(host: str, error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """A responder whose ``host`` fails with ``error`` and whose other hosts answer 200."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == host:
            raise error
        return httpx.Response(200, json={"host": request.url.host})

    return responder


def hosts(upstream: UpstreamStub) -> list[str]:
    return [request.url.host for request in upstream.requests]


async def test_spreads_requests_between_the_instances(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    responses = [await client.get("/api/agent/api/v1/demos") for _ in range(3)]

    assert hosts(upstream) == [HOST_A, HOST_B, HOST_A]
    assert [r.headers["x-gateway-instance"] for r in responses] == [ID_A, ID_B, ID_A]
    assert all(r.headers["x-agent-token"] == TOKEN for r in upstream.requests)


async def test_the_download_goes_back_to_the_instance_that_ran_the_chat(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: {}\n\n")
    )
    chat = await client.post("/api/agent/api/v1/chat", json={"question": "hi"})
    instance = chat.headers["x-gateway-instance"]

    for _ in range(2):
        download = await client.get(f"/api/agent@{instance}/api/v1/artifacts/abc/download")
        assert download.headers["x-gateway-instance"] == instance

    assert hosts(upstream) == [HOST_A, HOST_A, HOST_A]
    assert str(upstream.requests[-1].url) == f"http://{HOST_A}/api/v1/artifacts/abc/download"


async def test_an_unknown_instance_is_404(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    response = await client.get("/api/agent@0badc0de/api/v1/health")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "instance_not_found"
    assert upstream.requests == []


async def test_a_post_moves_on_when_an_instance_refuses_the_connection(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.responder = fail_on(HOST_A, httpx.ConnectError("connection refused"))

    response = await client.post("/api/agent/api/v1/chat", json={"question": "hi"})

    assert response.status_code == 200
    assert response.headers["x-gateway-instance"] == ID_B
    assert hosts(upstream) == [HOST_A, HOST_B]


async def test_a_post_is_never_repeated_once_it_may_have_arrived(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.responder = fail_on(HOST_A, httpx.ReadTimeout("no answer"))

    response = await client.post("/api/agent/api/v1/chat", json={"question": "hi"})

    assert response.status_code == 504
    assert hosts(upstream) == [HOST_A]


async def test_an_instance_that_keeps_failing_is_left_out(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.responder = fail_on(HOST_A, httpx.ConnectError("connection refused"))

    responses = [await client.get("/api/agent/api/v1/demos") for _ in range(6)]

    assert {r.status_code for r in responses} == {200}
    assert {r.headers["x-gateway-instance"] for r in responses} == {ID_B}
    # Each failure of A is a whole retry sequence; after FAILURE_THRESHOLD of them its circuit
    # opens and it is skipped without being called.
    assert hosts(upstream).count(HOST_A) == FAILURE_THRESHOLD * MAX_ATTEMPTS


async def test_every_instance_down_is_502(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.ConnectError("connection refused"))

    response = await client.post("/api/agent/api/v1/chat", json={"question": "hi"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "bad_gateway"
    assert hosts(upstream) == [HOST_A, HOST_B]


async def test_health_reports_every_instance(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.responder = fail_on(HOST_B, httpx.ConnectError("connection refused"))

    body = (await client.get("/health/services")).json()

    (agent,) = body["services"]
    assert body["status"] == "degraded"
    assert agent["status"] == "degraded"
    assert [(i["id"], i["status"]) for i in agent["instances"]] == [
        (ID_A, "up"),
        (ID_B, "down"),
    ]
    assert sorted(hosts(upstream)) == [HOST_A, HOST_B]
    assert all(r.headers["x-agent-token"] == TOKEN for r in upstream.requests)
