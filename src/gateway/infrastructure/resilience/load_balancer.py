import dataclasses
import logging

from gateway.domain.exceptions import CircuitOpenError, GatewayError, UpstreamError
from gateway.domain.models import (
    OutboundRequest,
    ServiceDefinition,
    ServiceInstance,
    UpstreamResponse,
)
from gateway.domain.ports import UpstreamClient

logger = logging.getLogger(__name__)


class LoadBalancingUpstreamClient(UpstreamClient):
    """Spreads the requests of each service across its instances, round-robin, with failover.

    Every request starts at the next instance in turn and moves on to the following one when
    the current one cannot take it: its circuit is open, or it failed in a way that makes
    sending the request again safe. Each instance is tried at most once per request.

    A request that already names its instance goes there and nowhere else: it is asking for
    something only that server has, such as an artifact kept in its memory.
    """

    def __init__(self, inner: UpstreamClient) -> None:
        self._inner = inner
        self._next_turn: dict[str, int] = {}

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        if request.instance is not None:
            return await self._send_to(request.instance, request)

        errors: list[GatewayError] = []
        for instance in self._rotation(request.service):
            try:
                return await self._send_to(instance, request)
            except CircuitOpenError as exc:
                errors.append(exc)
            except UpstreamError as exc:
                if not _can_fail_over(request, exc):
                    raise
                errors.append(exc)
                logger.warning(
                    "%s /%s failed on '%s@%s': %s",
                    request.method,
                    request.path.lstrip("/"),
                    request.service.name,
                    instance.id,
                    exc,
                )

        # An instance that was actually tried explains more than one whose circuit was open.
        tried = [error for error in errors if isinstance(error, UpstreamError)]
        raise (tried or errors)[-1]

    async def _send_to(
        self, instance: ServiceInstance, request: OutboundRequest
    ) -> UpstreamResponse:
        response = await self._inner.send(dataclasses.replace(request, instance=instance))
        return dataclasses.replace(response, instance_id=instance.id)

    def _rotation(self, service: ServiceDefinition) -> tuple[ServiceInstance, ...]:
        instances = service.instances
        start = self._next_turn.get(service.name, 0)
        self._next_turn[service.name] = (start + 1) % len(instances)
        return instances[start:] + instances[:start]


def _can_fail_over(request: OutboundRequest, error: UpstreamError) -> bool:
    """Another instance may repeat the request only if that cannot duplicate a side effect:
    the method is idempotent, or the failed instance never received the request."""
    return request.is_idempotent or not error.request_sent
