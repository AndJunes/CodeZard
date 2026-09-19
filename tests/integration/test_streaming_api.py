"""Server-Sent Events cross the gateway as they are produced, not once the service is done."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.integration.conftest import UpstreamStub

Message = dict[str, Any]

FIRST = b'data: {"type": "step"}\n\n'
SECOND = b'data: {"type": "done"}\n\n'
SSE_HEADERS = {"content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache"}


class UpstreamBody(httpx.AsyncByteStream):
    """An upstream body that holds its last chunk until ``release`` and can fail at the end."""

    def __init__(self, *chunks: bytes, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self._released = asyncio.Event()
        self.closed = False

    def release(self) -> None:
        self._released.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        *leading, last = self._chunks
        for chunk in leading:
            yield chunk
        await self._released.wait()
        yield last
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        # Closing a real connection waits on I/O: the point where a cancelled caller gives up.
        await asyncio.sleep(0.01)
        self.closed = True


class AsgiExchange:
    """Drives one request through the ASGI app by hand and exposes each message it sends.

    ``httpx.ASGITransport`` only returns once the whole body is ready, so it cannot tell a
    relayed stream from a buffered one.
    """

    def __init__(self, app: FastAPI, method: str, path: str) -> None:
        self._sent: asyncio.Queue[Message] = asyncio.Queue()
        self._request_read = False
        self._disconnected = asyncio.Event()
        scope = {
            "type": "http",
            # 2.3 is what uvicorn reports: Starlette then watches for disconnects in parallel.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"gateway.test")],
            "client": ("127.0.0.1", 50000),
            "server": ("gateway.test", 80),
        }
        self.task = asyncio.create_task(app(scope, self._receive, self._sent.put))

    async def _receive(self) -> Message:
        if not self._request_read:
            self._request_read = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    def disconnect(self) -> None:
        self._disconnected.set()

    async def next_message(self) -> Message:
        # A buffering gateway never sends the first chunk on its own: fail instead of hanging.
        return await asyncio.wait_for(self._sent.get(), timeout=2)


@pytest.fixture
async def running_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


async def test_relays_each_event_before_the_service_finishes(
    running_app: FastAPI, upstream: UpstreamStub
) -> None:
    body = UpstreamBody(FIRST, SECOND)
    upstream.responder = lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body)
    exchange = AsgiExchange(running_app, "POST", "/api/users/chat")

    start = await exchange.next_message()
    first = await exchange.next_message()
    body.release()
    second = await exchange.next_message()
    await asyncio.wait_for(exchange.task, timeout=2)

    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    assert (first["body"], first["more_body"]) == (FIRST, True)
    assert second["body"] == SECOND
    assert body.closed


async def test_releases_the_service_connection_when_the_client_goes_away(
    running_app: FastAPI, upstream: UpstreamStub
) -> None:
    body = UpstreamBody(FIRST, SECOND)
    upstream.responder = lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body)
    exchange = AsgiExchange(running_app, "POST", "/api/users/chat")
    await exchange.next_message()  # response start
    await exchange.next_message()  # first event; the service is now busy on the second

    exchange.disconnect()
    await asyncio.wait_for(exchange.task, timeout=2)

    assert body.closed


async def test_streamed_responses_keep_their_headers_and_skip_proxy_buffering(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    body = UpstreamBody(FIRST, SECOND)
    body.release()
    upstream.responder = lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body)

    response = await client.post("/api/users/chat", json={"question": "What is an index?"})

    assert response.status_code == 200
    assert response.content == FIRST + SECOND
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-length" not in response.headers
    assert len(response.headers["x-request-id"]) == 32
    # The service reads the body by its length, so it must still be sent.
    assert upstream.requests[0].headers["content-length"] == str(len(upstream.requests[0].content))


async def test_a_stream_that_breaks_midway_ends_early_and_is_logged(
    client: httpx.AsyncClient, upstream: UpstreamStub, caplog: pytest.LogCaptureFixture
) -> None:
    body = UpstreamBody(FIRST, error=httpx.ReadTimeout("timed out"))
    body.release()
    upstream.responder = lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body)

    with caplog.at_level(logging.WARNING, logger="gateway"):
        response = await client.post("/api/users/chat", json={})

    # The 200 was already on its way: what arrived is kept and the stream simply ends.
    assert response.status_code == 200
    assert response.content == FIRST
    assert "Stream from 'users' ended early" in caplog.text
    assert body.closed
