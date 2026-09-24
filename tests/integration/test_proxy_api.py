import asyncio
import gzip
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gateway.logging_config import request_id_var
from tests.integration.conftest import (
    FAILURE_THRESHOLD,
    MAX_ATTEMPTS,
    ORDERS_TOKEN,
    ChunkedBody,
    ClientConnection,
    UpstreamStub,
    http_scope,
)


async def test_forwards_get_requests_and_returns_the_upstream_response(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.Response(200, json={"id": 1}, headers={"x-custom": "yes"}))

    response = await client.get(
        "/api/users/items/1", params={"expand": "true"}, headers={"accept": "application/json"}
    )

    assert response.status_code == 200
    assert response.json() == {"id": 1}
    assert response.headers["x-custom"] == "yes"
    sent = upstream.requests[0]
    assert str(sent.url) == "http://users.internal/items/1?expand=true"
    assert sent.headers["accept"] == "application/json"
    assert sent.headers["x-forwarded-for"] == "127.0.0.1"
    assert sent.headers["x-forwarded-host"] == "gateway.test"
    assert sent.headers["x-forwarded-proto"] == "http"


@pytest.mark.parametrize(
    ("path", "expected_url"),
    [
        ("/api/orders/list", "http://orders.internal/v1/list"),
        ("/api/users", "http://users.internal/"),
        ("/api/users/", "http://users.internal/"),
    ],
)
async def test_maps_gateway_paths_to_service_urls(
    client: httpx.AsyncClient, upstream: UpstreamStub, path: str, expected_url: str
) -> None:
    await client.get(path)

    assert str(upstream.requests[0].url) == expected_url


async def test_accepts_the_slashes_of_the_path_percent_encoded(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    # What Swagger UI sends: it encodes the whole `path` parameter as a single segment.
    await client.get("/api/users/items%2F1")

    assert str(upstream.requests[0].url) == "http://users.internal/items/1"


async def test_forwards_request_bodies(client: httpx.AsyncClient, upstream: UpstreamStub) -> None:
    upstream.respond_with(httpx.Response(201, json={"id": 7}))

    response = await client.post("/api/users/items", json={"name": "Ada"})

    assert response.status_code == 201
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert sent.content == b'{"name":"Ada"}'
    assert sent.headers["content-type"] == "application/json"


async def test_injects_the_service_credential_and_ignores_the_client_one(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    await client.post("/api/orders/chat", json={}, headers={"x-orders-token": "forged"})
    await client.get("/api/users/items")

    to_orders, to_users = upstream.requests
    assert to_orders.headers.get_list("x-orders-token") == [ORDERS_TOKEN]
    # A credential belongs to its service: no other service ever receives it.
    assert "x-orders-token" not in to_users.headers


async def test_passes_upstream_client_errors_through_unchanged(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.Response(404, json={"detail": "user not found"}))

    response = await client.get("/api/users/items/99")

    assert response.status_code == 404
    assert response.json() == {"detail": "user not found"}


async def test_preserves_repeated_response_headers(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(
        httpx.Response(200, headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")])
    )

    response = await client.get("/api/users/login")

    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]


async def test_returns_decoded_bodies_without_stale_encoding_headers(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.responder = lambda _: httpx.Response(
        200, content=gzip.compress(b"hello"), headers={"content-encoding": "gzip"}
    )

    response = await client.get("/api/users/greeting")

    assert response.content == b"hello"
    assert "content-encoding" not in response.headers
    # No content-length: the body is forwarded as it arrives, so its size is not known
    # when the headers go out. Starlette frames it chunked instead.
    assert "content-length" not in response.headers


async def test_unknown_service_returns_404(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    response = await client.get("/api/billing/invoices", headers={"x-request-id": "rid-1"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "service_not_found",
            "message": "Service 'billing' is not registered",
            "request_id": "rid-1",
        }
    }
    assert upstream.requests == []


async def test_unreachable_service_returns_502_after_retrying(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.ConnectError("connection refused"))

    response = await client.get("/api/users/items")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "bad_gateway"
    assert len(upstream.requests) == MAX_ATTEMPTS


async def test_slow_service_returns_504(client: httpx.AsyncClient, upstream: UpstreamStub) -> None:
    upstream.respond_with(httpx.ReadTimeout("timed out"))

    response = await client.get("/api/users/items")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "gateway_timeout"


async def test_retries_transient_failures_on_idempotent_requests(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.Response(503), httpx.Response(200, json={"ok": True}))

    response = await client.get("/api/users/items")

    assert response.status_code == 200
    assert len(upstream.requests) == 2


async def test_never_retries_post_requests(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.Response(503))

    response = await client.post("/api/users/items", json={})

    assert response.status_code == 503
    assert len(upstream.requests) == 1


async def test_opens_the_circuit_after_repeated_failures(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.ConnectError("connection refused"))
    for _ in range(FAILURE_THRESHOLD):
        assert (await client.get("/api/users/items")).status_code == 502

    response = await client.get("/api/users/items")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert len(upstream.requests) == FAILURE_THRESHOLD * MAX_ATTEMPTS
    # Other services keep working.
    upstream.respond_with(httpx.Response(200))
    assert (await client.get("/api/orders/list")).status_code == 200


async def test_generates_a_request_id_and_propagates_it(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    response = await client.get("/api/users/items")

    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert upstream.requests[0].headers["x-request-id"] == request_id


async def test_reuses_a_valid_incoming_request_id(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    response = await client.get("/api/users/items", headers={"x-request-id": "abc-123"})

    assert response.headers["x-request-id"] == "abc-123"
    assert upstream.requests[0].headers["x-request-id"] == "abc-123"


@pytest.mark.parametrize("incoming", ["has spaces", "x" * 129, "semi;colon"])
async def test_replaces_unsafe_incoming_request_ids(
    client: httpx.AsyncClient, incoming: str
) -> None:
    response = await client.get("/api/users/items", headers={"x-request-id": incoming})

    assert response.headers["x-request-id"] != incoming


async def test_unexpected_errors_return_a_generic_500(app: FastAPI) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client,
    ):
        response = await client.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text


async def test_streamed_responses_carry_no_content_length(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    upstream.respond_with(httpx.Response(200, content=b"hello"))

    response = await client.get("/api/users/greeting")

    assert response.content == b"hello"
    # The size is unknown when the headers go out, so the framing is chunked instead.
    assert "content-length" not in response.headers
    assert response.headers["x-request-id"]


async def test_forwards_each_chunk_as_it_arrives_instead_of_waiting_for_the_last(
    started_app: FastAPI, upstream: UpstreamStub
) -> None:
    """The reason this feature exists, asserted as an ordering rather than a duration.

    A service that reports progress over minutes is useless behind a gateway that waits
    for the end. What is proven here is that the first chunk leaves the gateway *while the
    upstream has not produced the last one yet*.
    """
    released = asyncio.Event()
    arrived = asyncio.Event()
    body_chunks: list[bytes] = []

    async def slow_body() -> AsyncIterator[bytes]:
        yield b"data: first\n\n"
        await released.wait()
        yield b"data: last\n\n"

    upstream.responder = lambda _: httpx.Response(
        200, stream=ChunkedBody(slow_body()), headers={"content-type": "text/event-stream"}
    )

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            body_chunks.append(bytes(message["body"]))  # type: ignore[arg-type]
            arrived.set()

    call = asyncio.create_task(
        started_app(http_scope("/api/users/events"), ClientConnection(), send)
    )

    await asyncio.wait_for(arrived.wait(), timeout=2)
    assert body_chunks == [b"data: first\n\n"]
    assert not released.is_set()

    released.set()
    await asyncio.wait_for(call, timeout=2)
    assert body_chunks == [b"data: first\n\n", b"data: last\n\n"]


async def test_closes_the_upstream_connection_once_the_response_is_sent(
    started_app: FastAPI, upstream: UpstreamStub
) -> None:
    """Every streamed response must give its connection back to the pool."""
    closed = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        try:
            yield b"first"
            yield b"second"
        finally:
            closed.set()

    upstream.responder = lambda _: httpx.Response(200, stream=ChunkedBody(body()))

    async def send(_: dict[str, object]) -> None:
        return

    await started_app(http_scope("/api/users/events"), ClientConnection(), send)

    assert closed.is_set()


async def test_logs_emitted_while_the_body_streams_still_carry_the_request_id(
    started_app: FastAPI, upstream: UpstreamStub, caplog: pytest.LogCaptureFixture
) -> None:
    """The request id has to survive as long as the response does.

    With ``BaseHTTPMiddleware`` the context was released when the headers went out, so
    anything logged while chunks were still being pumped came out attributed to nobody —
    which is precisely when a long stream is worth diagnosing.
    """
    seen: list[str | None] = []

    async def body() -> AsyncIterator[bytes]:
        yield b"first"
        seen.append(request_id_var.get())
        yield b"last"

    upstream.responder = lambda _: httpx.Response(200, stream=ChunkedBody(body()))

    async def send(_: dict[str, object]) -> None:
        return

    scope = http_scope("/api/users/events")
    scope["headers"] = [(b"host", b"gateway.test"), (b"x-request-id", b"abc123")]
    await started_app(scope, ClientConnection(), send)

    assert seen == ["abc123"]
