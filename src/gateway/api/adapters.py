"""Conversions between Starlette request/response objects and domain models."""

from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from gateway.domain.models import InboundRequest, UpstreamResponse, UpstreamStream


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
    return response


def to_streaming_response(upstream: UpstreamStream) -> Response:
    """Forward the body as it arrives instead of after it is complete.

    ``background`` is what returns the connection to the pool: Starlette runs it once the
    response has been sent, whether the client read it all or hung up halfway. Without it
    every request would leak a connection.
    """
    response = StreamingResponse(
        upstream.chunks,
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    _copy_headers(upstream.headers, response)
    return response


def _copy_headers(headers: tuple[tuple[str, str], ...], response: Response) -> None:
    for name, value in headers:
        # append (not set) keeps repeated headers such as Set-Cookie.
        response.headers.append(name, value)
