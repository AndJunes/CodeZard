"""End-to-end fixtures: the real app, with only the network to downstream services faked."""

from collections import deque
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

from gateway.api.app import create_app
from gateway.config.settings import (
    CircuitBreakerSettings,
    RetrySettings,
    ServiceSettings,
    Settings,
)

MAX_ATTEMPTS = 3
FAILURE_THRESHOLD = 2
ORDERS_TOKEN = "orders-shared-secret"


class UpstreamStub:
    """Plays the role of every downstream service and records what it receives.

    Use ``respond_with`` for plain scripted responses (the last one repeats), or
    assign ``responder`` directly for anything else (per-host logic, encoded bodies).
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(
            200, json={"ok": True}
        )

    def respond_with(self, *outcomes: httpx.Response | Exception) -> None:
        queue = deque(outcomes)

        def responder(_: httpx.Request) -> httpx.Response:
            outcome = queue.popleft() if len(queue) > 1 else queue[0]
            if isinstance(outcome, Exception):
                raise outcome
            # A fresh copy per call: httpx responses cannot be reused across requests.
            return httpx.Response(
                outcome.status_code, headers=outcome.headers, content=outcome.content
            )

        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


@pytest.fixture
def upstream() -> UpstreamStub:
    return UpstreamStub()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        services=[
            ServiceSettings(name="users", base_url="http://users.internal"),
            ServiceSettings(
                name="orders",
                base_url="http://orders.internal/v1",
                health_path="/status",
                headers={"X-Orders-Token": ORDERS_TOKEN},
            ),
        ],
        retry=RetrySettings(max_attempts=MAX_ATTEMPTS, base_delay_seconds=0),
        circuit_breaker=CircuitBreakerSettings(
            failure_threshold=FAILURE_THRESHOLD, recovery_timeout_seconds=60
        ),
    )


@pytest.fixture
def app(settings: Settings, upstream: UpstreamStub) -> FastAPI:
    return create_app(
        settings,
        http_client_factory=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client,
    ):
        yield client
