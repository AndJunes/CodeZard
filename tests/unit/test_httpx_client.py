from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamError, UpstreamTimeoutError
from gateway.domain.models import OutboundRequest, ServiceDefinition
from gateway.infrastructure.httpx_client import HttpxUpstreamClient
from tests.fakes import make_request

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def captured() -> list[httpx.Request]:
    return []


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[[Handler], HttpxUpstreamClient]]:
    opened: list[httpx.AsyncClient] = []

    def factory(handler: Handler) -> HttpxUpstreamClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(http)
        return HttpxUpstreamClient(http)

    yield factory
    for http in opened:
        await http.aclose()


async def test_forwards_method_url_query_headers_and_body(
    users_service: ServiceDefinition,
    captured: list[httpx.Request],
    make_client: Callable[[Handler], HttpxUpstreamClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201, headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")], content=b"created"
        )

    response = await make_client(handler).send(
        OutboundRequest(
            service=users_service,
            method="POST",
            path="/items",
            headers=(("x-api-key", "k"),),
            query_params=(("tag", "a"), ("tag", "b")),
            body=b'{"x": 1}',
        )
    )

    sent = captured[0]
    assert sent.method == "POST"
    assert str(sent.url) == "http://users.internal/items?tag=a&tag=b"
    assert sent.headers["x-api-key"] == "k"
    assert sent.content == b'{"x": 1}'
    assert response.status_code == 201
    assert response.body == b"created"
    assert [value for name, value in response.headers if name == "set-cookie"] == ["a=1", "b=2"]


async def test_applies_the_service_timeout(
    users_service: ServiceDefinition,
    captured: list[httpx.Request],
    make_client: Callable[[Handler], HttpxUpstreamClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    await make_client(handler).send(OutboundRequest(service=users_service, method="GET", path="/"))

    assert captured[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), users_service.timeout_seconds
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectTimeout("timed out"), UpstreamTimeoutError),
        (httpx.ReadTimeout("timed out"), UpstreamTimeoutError),
        (httpx.ConnectError("refused"), UpstreamConnectionError),
        (httpx.RemoteProtocolError("bad response"), UpstreamConnectionError),
    ],
)
async def test_translates_transport_errors_into_domain_errors(
    users_service: ServiceDefinition,
    make_client: Callable[[Handler], HttpxUpstreamClient],
    error: httpx.TransportError,
    expected: type[UpstreamError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(expected) as exc_info:
        await make_client(handler).send(
            OutboundRequest(service=users_service, method="GET", path="/")
        )

    assert exc_info.value.service_name == "users"
    assert exc_info.value.__cause__ is error


async def test_stream_returns_before_the_body_is_read(
    make_client: Callable[[Handler], HttpxUpstreamClient],
    users_service: ServiceDefinition,
) -> None:
    """The point of streaming: the status is known long before the last byte."""
    produced: list[str] = []

    async def body() -> AsyncIterator[bytes]:
        produced.append("first")
        yield b"first"
        produced.append("second")
        yield b"second"

    client = make_client(lambda _: httpx.Response(200, stream=_Stream(body())))

    stream = await client.stream(make_request(users_service))

    assert stream.status_code == 200
    assert produced == []  # nothing was pulled from the body yet

    assert [chunk async for chunk in stream.chunks] == [b"first", b"second"]
    await stream.aclose()


async def test_stream_translates_transport_errors_into_domain_errors(
    make_client: Callable[[Handler], HttpxUpstreamClient],
    users_service: ServiceDefinition,
) -> None:
    """Only up to the headers: past them there is no error left to translate."""

    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = make_client(fail)

    with pytest.raises(UpstreamConnectionError):
        await client.stream(make_request(users_service))


async def test_the_read_timeout_can_outlast_the_connect_timeout(
    captured: list[httpx.Request],
    make_client: Callable[[Handler], HttpxUpstreamClient],
) -> None:
    """A stream needs a long gap between chunks and a short handshake.

    A single scalar cannot say both, and saying it once says it for all four channels.
    """
    service = ServiceDefinition(
        name="events",
        base_url="http://events.internal",
        timeout_seconds=2.0,
        read_timeout_seconds=600.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    await make_client(handler).stream(make_request(service))

    assert captured[0].extensions["timeout"] == {
        "connect": 2.0,
        "read": 600.0,
        "write": 2.0,
        "pool": 2.0,
    }


class _Stream(httpx.AsyncByteStream):
    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            yield chunk
