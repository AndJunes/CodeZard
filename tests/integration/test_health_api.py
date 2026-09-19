import httpx

from tests.integration.conftest import ORDERS_TOKEN, UpstreamStub


async def test_liveness(client: httpx.AsyncClient, upstream: UpstreamStub) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert upstream.requests == []


async def test_services_health_is_ok_when_every_service_is_up(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    response = await client.get("/health/services")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert {service["name"]: service["status"] for service in body["services"]} == {
        "users": "up",
        "orders": "up",
    }
    assert sorted(str(request.url) for request in upstream.requests) == [
        "http://orders.internal/v1/status",
        "http://users.internal/health",
    ]


async def test_health_checks_carry_the_service_credential(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    await client.get("/health/services")

    by_host = {request.url.host: request for request in upstream.requests}
    assert by_host["orders.internal"].headers["x-orders-token"] == ORDERS_TOKEN
    assert "x-orders-token" not in by_host["users.internal"].headers


async def test_services_health_is_degraded_when_a_service_is_down(
    client: httpx.AsyncClient, upstream: UpstreamStub
) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "orders.internal":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    upstream.responder = responder

    response = await client.get("/health/services")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    services = {service["name"]: service for service in body["services"]}
    assert services["users"]["status"] == "up"
    assert isinstance(services["users"]["latency_ms"], float)
    assert services["orders"] == {
        "name": "orders",
        "status": "down",
        "latency_ms": None,
        "detail": "Service 'orders' is unreachable",
    }
