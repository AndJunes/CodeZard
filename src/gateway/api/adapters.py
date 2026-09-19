"""Conversions between Starlette request/response objects and domain models."""

import logging
from collections.abc import AsyncIterator

import anyio
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from gateway.domain.exceptions import UpstreamError
from gateway.domain.models import ByteStream, InboundRequest, UpstreamResponse

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
    response: Response
    if upstream.stream is None:
        response = Response(content=upstream.body, status_code=upstream.status_code)
    else:
        response = StreamingResponse(_relay(upstream.stream), status_code=upstream.status_code)
        # Tells a reverse proxy in front (nginx and the like) not to buffer the stream either.
        response.headers["x-accel-buffering"] = "no"
    for name, value in upstream.headers:
        # append (not set) keeps repeated headers such as Set-Cookie.
        response.headers.append(name, value)
    if upstream.instance_id is not None:
        # Set after the service's headers, so a service cannot pass itself off as another one.
        response.headers[INSTANCE_HEADER] = upstream.instance_id
    return response


async def _relay(stream: ByteStream) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream:
            yield chunk
    except UpstreamError as exc:
        # The status line is already sent, so all that is left is to end the stream early.
        logger.warning("Stream from '%s' ended early: %s", exc.service_name, exc)
    finally:
        # Shielded: when the client disconnects this runs inside a cancelled scope, and the
        # upstream connection must be released anyway.
        with anyio.CancelScope(shield=True):
            await stream.aclose()
