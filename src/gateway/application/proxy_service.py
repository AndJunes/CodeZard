"""Use case: forward a client request to the microservice that owns it."""

import dataclasses
import logging

from gateway.application.header_policy import HeaderPolicy
from gateway.domain.models import InboundRequest, OutboundRequest, UpstreamResponse
from gateway.domain.ports import ServiceRegistry, UpstreamClient

logger = logging.getLogger(__name__)


class ProxyService:
    def __init__(
        self,
        registry: ServiceRegistry,
        client: UpstreamClient,
        header_policy: HeaderPolicy,
    ) -> None:
        self._registry = registry
        self._client = client
        self._header_policy = header_policy

    async def forward(self, service_name: str, inbound: InboundRequest) -> UpstreamResponse:
        service = self._registry.get(service_name)
        outbound = OutboundRequest(
            service=service,
            method=inbound.method,
            path=inbound.path,
            headers=self._header_policy.for_upstream(inbound),
            query_params=inbound.query_params,
            body=inbound.body,
        )
        logger.debug("Forwarding %s %s", outbound.method, outbound.url)

        response = await self._client.send(outbound)
        return dataclasses.replace(
            response, headers=self._header_policy.for_client(response.headers)
        )
