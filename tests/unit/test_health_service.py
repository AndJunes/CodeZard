from gateway.application.health_service import HealthService
from gateway.domain.exceptions import UpstreamConnectionError
from gateway.domain.models import (
    HealthStatus,
    OutboundRequest,
    ServiceDefinition,
    ServiceHealth,
    UpstreamResponse,
)
from gateway.domain.ports import UpstreamClient
from gateway.infrastructure.registry import InMemoryServiceRegistry
from tests.fakes import ScriptedUpstreamClient


class ClientPerService(UpstreamClient):
    def __init__(self, clients: dict[str, ScriptedUpstreamClient]) -> None:
        self._clients = clients

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        return await self._clients[request.service.name].send(request)


async def test_healthy_service_is_up_with_latency(users_service: ServiceDefinition) -> None:
    client = ScriptedUpstreamClient(UpstreamResponse(status_code=204))
    service = HealthService(
        InMemoryServiceRegistry([users_service]), client, timer=iter([1.0, 1.25]).__next__
    )

    health = await service.check(users_service)

    assert health == ServiceHealth(name="users", status=HealthStatus.UP, latency_ms=250.0)
    assert (client.requests[0].method, client.requests[0].path) == ("GET", "/health")


async def test_error_status_marks_the_service_down(users_service: ServiceDefinition) -> None:
    client = ScriptedUpstreamClient(UpstreamResponse(status_code=500))
    service = HealthService(InMemoryServiceRegistry([users_service]), client)

    health = await service.check(users_service)

    assert health.status is HealthStatus.DOWN
    assert health.detail == "Unexpected status code 500"


async def test_unreachable_service_is_down(users_service: ServiceDefinition) -> None:
    client = ScriptedUpstreamClient(UpstreamConnectionError("users"))
    service = HealthService(InMemoryServiceRegistry([users_service]), client)

    health = await service.check(users_service)

    assert health == ServiceHealth(
        name="users", status=HealthStatus.DOWN, detail="Service 'users' is unreachable"
    )


async def test_check_all_reports_every_registered_service(
    users_service: ServiceDefinition, orders_service: ServiceDefinition
) -> None:
    client = ClientPerService(
        {
            "users": ScriptedUpstreamClient(UpstreamResponse(status_code=200)),
            "orders": ScriptedUpstreamClient(UpstreamConnectionError("orders")),
        }
    )
    service = HealthService(InMemoryServiceRegistry([users_service, orders_service]), client)

    report = await service.check_all()

    assert [(h.name, h.status) for h in report.services] == [
        ("users", HealthStatus.UP),
        ("orders", HealthStatus.DOWN),
    ]
    assert not report.is_healthy
