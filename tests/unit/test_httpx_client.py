from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamError, UpstreamTimeoutError
from gateway.domain.models import OutboundRequest, ServiceDefinition
from gateway.infrastructure.httpx_client import HttpxUpstreamClient

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
