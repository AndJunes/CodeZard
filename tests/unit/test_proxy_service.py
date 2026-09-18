import pytest

from gateway.application.header_policy import HeaderPolicy
from gateway.application.proxy_service import ProxyService
from gateway.domain.exceptions import ServiceNotFoundError, UpstreamTimeoutError
from gateway.domain.models import InboundRequest, ServiceDefinition, UpstreamResponse
from gateway.infrastructure.registry import InMemoryServiceRegistry
from tests.fakes import ScriptedUpstreamClient

GET_ITEMS = InboundRequest(method="GET", path="/items")


def proxy(service: ServiceDefinition, client: ScriptedUpstreamClient) -> ProxyService:
    return ProxyService(InMemoryServiceRegistry([service]), client, HeaderPolicy())


async def test_forwards_the_request_to_the_resolved_service(
    users_service: ServiceDefinition,
) -> None:
    client = ScriptedUpstreamClient(UpstreamResponse(status_code=200))
    inbound = InboundRequest(
        method="PUT",
        path="/items/1",
        headers=(("accept", "application/json"), ("host", "gateway.local")),
        query_params=(("q", "1"),),
        body=b"data",
        client_host="10.0.0.1",
        request_id="rid-1",
    )

    await proxy(users_service, client).forward("users", inbound)

    sent = client.requests[0]
    assert sent.service is users_service
    assert (sent.method, sent.path, sent.body) == ("PUT", "/items/1", b"data")
    assert sent.query_params == (("q", "1"),)
    assert ("accept", "application/json") in sent.headers
    assert ("x-request-id", "rid-1") in sent.headers
    assert "host" not in dict(sent.headers)


async def test_returns_the_upstream_response_with_filtered_headers(
    users_service: ServiceDefinition,
) -> None:
    client = ScriptedUpstreamClient(
        UpstreamResponse(
            status_code=201,
            headers=(
                ("content-type", "application/json"),
                ("content-length", "2"),
                ("connection", "close"),
            ),
            body=b"{}",
        )
    )

    response = await proxy(users_service, client).forward("users", GET_ITEMS)

    assert response == UpstreamResponse(
        status_code=201, headers=(("content-type", "application/json"),), body=b"{}"
    )


async def test_unknown_service_fails_without_calling_upstream(
    users_service: ServiceDefinition,
) -> None:
    client = ScriptedUpstreamClient(UpstreamResponse(status_code=200))

    with pytest.raises(ServiceNotFoundError):
        await proxy(users_service, client).forward("billing", GET_ITEMS)

    assert client.calls == 0


async def test_propagates_upstream_errors(users_service: ServiceDefinition) -> None:
    client = ScriptedUpstreamClient(UpstreamTimeoutError("users"))

    with pytest.raises(UpstreamTimeoutError):
        await proxy(users_service, client).forward("users", GET_ITEMS)
