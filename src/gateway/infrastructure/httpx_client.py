from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import httpx

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamTimeoutError
from gateway.domain.models import ByteStream, OutboundRequest, UpstreamResponse
from gateway.domain.ports import UpstreamClient

# Server-Sent Events only make sense as they happen: buffering them would hold every event
# back until the service closes the stream.
_STREAMED_MEDIA_TYPES = frozenset({"text/event-stream"})

# Raised before a single byte of the request left: no connection, or none free in the pool.
_NOT_SENT_TIMEOUTS = (httpx.ConnectTimeout, httpx.PoolTimeout)


class HttpxUpstreamClient(UpstreamClient):
    """Sends requests over a shared ``httpx.AsyncClient`` (pooled connections)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        service_name = request.service.name
        http_request = self._client.build_request(
            request.method,
            request.url,
            params=request.query_params,
            headers=list(request.headers),
            content=request.body,
            timeout=request.service.timeout_seconds,
        )
        with _translated_errors(service_name):
            response = await self._client.send(http_request, stream=True)

        headers = tuple(response.headers.multi_items())
        if _media_type(response) in _STREAMED_MEDIA_TYPES:
            return UpstreamResponse(
                status_code=response.status_code,
                headers=headers,
                stream=HttpxByteStream(response, service_name),
            )

        try:
            with _translated_errors(service_name):
                body = await response.aread()
        finally:
            await response.aclose()
        return UpstreamResponse(status_code=response.status_code, headers=headers, body=body)


class HttpxByteStream(ByteStream):
    """The body of an httpx response opened with ``stream=True``.

    The service timeout applies to each wait for the next chunk, not to the whole body, so a
    long stream is fine as long as the service keeps sending.
    """

    def __init__(self, response: httpx.Response, service_name: str) -> None:
        self._response = response
        self._service_name = service_name

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _translated_errors(self._service_name):
            async for chunk in self._response.aiter_bytes():
                yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


@contextmanager
def _translated_errors(service_name: str) -> Iterator[None]:
    """Turn httpx failures into domain errors so callers never depend on httpx."""
    try:
        yield
    # Subclasses first: the connect and pool timeouts are TimeoutExceptions, and every
    # TimeoutException is a TransportError.
    except _NOT_SENT_TIMEOUTS as exc:
        raise UpstreamTimeoutError(service_name, request_sent=False) from exc
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError(service_name) from exc
    except httpx.ConnectError as exc:
        raise UpstreamConnectionError(service_name, request_sent=False) from exc
    except httpx.TransportError as exc:
        raise UpstreamConnectionError(service_name) from exc


def _media_type(response: httpx.Response) -> str:
    content_type: str = response.headers.get("content-type", "")
    return content_type.partition(";")[0].strip().lower()
