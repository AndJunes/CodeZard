"""Conversions between Starlette request/response objects and domain models."""

import logging
from collections.abc import AsyncIterator

import anyio
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from gateway.domain.exceptions import UpstreamError
from gateway.domain.models import InboundRequest, UpstreamResponse, UpstreamStream

logger = logging.getLogger(__name__)

INSTANCE_HEADER = "x-gateway-instance"
"""Names the instance that answered: ``/api/{service}@{instance}/...`` reaches it again."""


async def to_inbound_request(request: Request, path: str) -> InboundRequest:
    return InboundRequest(
        method=request.method,
        path=path,
        headers=tuple(request.headers.items()),
        query_params=tuple(request.query_params.multi_items()),
        body=await request.body(),
        client_host=request.client.host if request.client else None,
        scheme=request.url.scheme,
        host=request.headers.get("host"),
        request_id=getattr(request.state, "request_id", None),
    )


def to_response(upstream: UpstreamResponse) -> Response:
    response = Response(content=upstream.body, status_code=upstream.status_code)
    _copy_headers(upstream.headers, response)
    _copy_instance_header(upstream.instance_id, response)
    return response


def to_streaming_response(upstream: UpstreamStream) -> Response:
    """Forward the body as it arrives instead of after it is complete.

    ``background`` is what returns the connection to the pool: Starlette runs it once the
    response has been sent, whether the client read it all or hung up halfway. Without it
    every request would leak a connection.
    """
    response = StreamingResponse(_relay(upstream), status_code=upstream.status_code)
    # Prevent reverse proxies such as nginx from buffering a live stream.
    response.headers["x-accel-buffering"] = "no"
    _copy_headers(upstream.headers, response)
    _copy_instance_header(upstream.instance_id, response)
    return response


def _copy_headers(headers: tuple[tuple[str, str], ...], response: Response) -> None:
    for name, value in headers:
        # append (not set) keeps repeated headers such as Set-Cookie.
        response.headers.append(name, value)


def _copy_instance_header(instance_id: str | None, response: Response) -> None:
    if instance_id is not None:
        # Set after the service's headers, so a service cannot pass itself off as another one.
        response.headers[INSTANCE_HEADER] = instance_id


async def _relay(upstream: UpstreamStream) -> AsyncIterator[bytes]:
    try:
        async for chunk in upstream.chunks:
            yield chunk
    except UpstreamError as exc:
        # The status line is already sent, so all that is left is to end the stream early.
        logger.warning("Stream from '%s' ended early: %s", exc.service_name, exc)
    finally:
        # A disconnected client cancels this scope; the upstream connection still must close.
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
