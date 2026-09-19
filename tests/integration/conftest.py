"""End-to-end fixtures: the real app, with only the network to downstream services faked."""

import asyncio
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


class ChunkedBody(httpx.AsyncByteStream):
    """Wraps an async generator so ``httpx.Response`` accepts it as a streaming body.

    ``httpx.Response(stream=...)`` requires an ``AsyncByteStream``, not a bare generator.
    Streaming tests need a body they can release chunk by chunk, which is the only way to
    prove the gateway forwards the first one before the last one exists.
    """

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            yield chunk


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
                name="orders", base_url="http://orders.internal/v1", health_path="/status"
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


@pytest.fixture
async def started_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    """The app with its lifespan entered, to be driven as raw ASGI.

    Needed because ``httpx.ASGITransport`` collects every ``http.response.body`` message
    and only builds the response once the last one arrives. That is fine for asserting on
    status, headers and content, but it makes streaming invisible: through it, a gateway
    that buffers and one that does not look identical. Tests about *when* bytes come out
    have to watch the ASGI messages themselves.
    """
    async with app.router.lifespan_context(app):
        yield app


def http_scope(path: str, method: str = "GET") -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"gateway.test")],
        "client": ("127.0.0.1", 12345),
        "server": ("gateway.test", 80),
    }


class ClientConnection:
    """A ``receive`` callable that behaves like a real server's.

    The body arrives once; after that it does not answer again until the client hangs up.
    That matters only for streamed responses: ``StreamingResponse`` watches for a
    disconnect with ``while True: await receive()``, so a receive that keeps returning
    ``http.request`` immediately turns that watch into a busy loop and starves the event
    loop. A buffered response never listens, which is why this only shows up now.
    """

    def __init__(self, body: bytes = b"") -> None:
        self._body = body
        self._delivered = False
        self._hung_up = asyncio.Event()

    def hang_up(self) -> None:
        self._hung_up.set()

    async def __call__(self) -> dict[str, object]:
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._hung_up.wait()
        return {"type": "http.disconnect"}
