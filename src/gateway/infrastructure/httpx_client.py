import httpx

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamTimeoutError
from gateway.domain.models import OutboundRequest, UpstreamResponse
from gateway.domain.ports import UpstreamClient


class HttpxUpstreamClient(UpstreamClient):
    """Sends requests over a shared ``httpx.AsyncClient`` (pooled connections)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        service_name = request.service.name
        try:
            response = await self._client.request(
                request.method,
                request.url,
                params=request.query_params,
                headers=list(request.headers),
                content=request.body,
                timeout=request.service.timeout_seconds,
            )
        # TimeoutException subclasses TransportError, so it must be caught first.
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(service_name) from exc
        except httpx.TransportError as exc:
            raise UpstreamConnectionError(service_name) from exc

        return UpstreamResponse(
            status_code=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=response.content,
        )
