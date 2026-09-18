"""Use case: report the health of every downstream service."""

import asyncio
import time
from collections.abc import Callable

from gateway.domain.exceptions import UpstreamError
from gateway.domain.models import (
    HealthReport,
    HealthStatus,
    OutboundRequest,
    ServiceDefinition,
    ServiceHealth,
)
from gateway.domain.ports import ServiceRegistry, UpstreamClient


class HealthService:
    def __init__(
        self,
        registry: ServiceRegistry,
        client: UpstreamClient,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._registry = registry
        self._client = client
        self._timer = timer

    async def check_all(self) -> HealthReport:
        results = await asyncio.gather(*(self.check(s) for s in self._registry.all()))
        return HealthReport(services=tuple(results))

    async def check(self, service: ServiceDefinition) -> ServiceHealth:
        request = OutboundRequest(service=service, method="GET", path=service.health_path)
        started = self._timer()
        try:
            response = await self._client.send(request)
        except UpstreamError as exc:
            return ServiceHealth(name=service.name, status=HealthStatus.DOWN, detail=str(exc))

        latency_ms = round((self._timer() - started) * 1000, 2)
        if 200 <= response.status_code < 300:
            return ServiceHealth(name=service.name, status=HealthStatus.UP, latency_ms=latency_ms)
        return ServiceHealth(
            name=service.name,
            status=HealthStatus.DOWN,
            latency_ms=latency_ms,
            detail=f"Unexpected status code {response.status_code}",
        )
