"""Conversions between Starlette request/response objects and domain models."""

from fastapi import Request, Response

from gateway.domain.models import InboundRequest, UpstreamResponse


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
    for name, value in upstream.headers:
        # append (not set) keeps repeated headers such as Set-Cookie.
        response.headers.append(name, value)
    return response
