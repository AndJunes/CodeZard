from collections.abc import AsyncIterator

import httpx

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamTimeoutError
from gateway.domain.models import OutboundRequest, UpstreamResponse, UpstreamStream
from gateway.domain.ports import UpstreamClient

# Raised before a single byte of the request left: no connection, or none free in the pool.
_NOT_SENT_TIMEOUTS = (httpx.ConnectTimeout, httpx.PoolTimeout)


def _timeout(request: OutboundRequest) -> httpx.Timeout:
    """Per-channel timeouts, because "read" means two different things here.

    A scalar expands to the same value on all four channels. That is fine for a buffered
    body, where read bounds the whole download. On a stream it bounds the *gap* between
    chunks, and a service pushing events for minutes needs a long gap without also being
    given minutes to complete a TCP handshake.
    """
    service = request.service
    return httpx.Timeout(
        connect=service.timeout_seconds,
        read=service.read_timeout,
        write=service.timeout_seconds,
        pool=service.timeout_seconds,
    )


def _as_domain_error(request: OutboundRequest, exc: httpx.TransportError) -> Exception:
    # TimeoutException subclasses TransportError, so it must be checked first.
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamTimeoutError(
            request.service.name, request_sent=not isinstance(exc, _NOT_SENT_TIMEOUTS)
        )
    return UpstreamConnectionError(
        request.service.name, request_sent=not isinstance(exc, httpx.ConnectError)
    )


class HttpxUpstreamClient(UpstreamClient):
    """Sends requests over a shared ``httpx.AsyncClient`` (pooled connections)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        try:
            response = await self._client.request(
                request.method,
                request.url,
                params=request.query_params,
                headers=list(request.headers),
                content=request.body,
                timeout=_timeout(request),
            )
        except httpx.TransportError as exc:
            raise _as_domain_error(request, exc) from exc

        return UpstreamResponse(
            status_code=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=response.content,
        )

    async def stream(self, request: OutboundRequest) -> UpstreamStream:
        outgoing = self._client.build_request(
            request.method,
            request.url,
            params=request.query_params,
            headers=list(request.headers),
            content=request.body,
            timeout=_timeout(request),
        )
        try:
            response = await self._client.send(outgoing, stream=True)
        except httpx.TransportError as exc:
            raise _as_domain_error(request, exc) from exc

        # aiter_bytes and not aiter_raw: it decodes, which is exactly what
        # `HeaderPolicy.for_client` already assumes when it drops `content-encoding`.
        # Raw bytes would forward a body the client is no longer told how to decode.
        return UpstreamStream(
            status_code=response.status_code,
            headers=tuple(response.headers.multi_items()),
            chunks=_translated_chunks(response, request),
            aclose=response.aclose,
        )


async def _translated_chunks(
    response: httpx.Response, request: OutboundRequest
) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.TransportError as exc:
        raise _as_domain_error(request, exc) from exc
