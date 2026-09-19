"""Catch-all routes: ``/api/{service}/{path}`` is forwarded to ``{service_base_url}/{path}``."""

from typing import Annotated

from fastapi import APIRouter, Path, Request, Response

from gateway.api.adapters import to_inbound_request, to_response
from gateway.api.dependencies import ProxyServiceDep
from gateway.api.openapi import JsonObject, gateway_error_responses
from gateway.application.proxy_service import ProxyService

PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# `/api/{service}@{instance}/...` pins a request to one instance. Service names cannot hold it.
INSTANCE_SEPARATOR = "@"

# Proxied like the rest, but kept out of Swagger: they would add entries and no information.
_UNDOCUMENTED_METHODS = frozenset({"HEAD", "OPTIONS"})
_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})

_DESCRIPTION = """\
Forwards the request to `{base_url of service_name}/{path}`: same method, query string, body
and headers, minus the hop-by-hop ones. The gateway adds `X-Forwarded-For`, `X-Forwarded-Proto`,
`X-Forwarded-Host`, `X-Request-ID` and the headers configured for the service, which replace
any client header of the same name.

`/api/{service_name}` alone (no trailing path) reaches the root of the service.

A service with several instances gets its requests spread among them, and a request moves on
to the next instance when the current one cannot take it. The `X-Gateway-Instance` response
header names the instance that answered; `{service_name}@{instance}` sends a request to that
instance and no other, e.g. to download something it keeps in memory.
"""

_ANY_BODY = {
    "requestBody": {
        "required": False,
        "description": "Forwarded byte for byte, whatever its content type.",
        "content": {"application/json": {"schema": {}, "example": {"name": "Ada"}}},
    }
}

_RESPONSES: dict[int | str, JsonObject] = {
    "200": {
        "description": "The service's response, relayed unchanged.",
        "headers": {
            "X-Gateway-Instance": {
                "description": "The instance that answered.",
                "schema": {"type": "string", "example": "3fa1c2d0"},
            }
        },
    },
    **gateway_error_responses(404, 502, 503, 504),
    "default": {"description": "Any other status the service answers, relayed unchanged."},
}

ServiceName = Annotated[
    str,
    Path(
        description="A service registered in `GATEWAY_SERVICES`, optionally pinned to one of "
        "its instances: `name@instance`.",
        examples=["httpbin"],
    ),
]
ServicePath = Annotated[
    str,
    Path(description="The path inside the service. It may contain `/`.", examples=["anything/1"]),
]

router = APIRouter(prefix="/api", tags=["proxy"])


async def proxy_path(
    service_name: ServiceName, path: ServicePath, request: Request, proxy: ProxyServiceDep
) -> Response:
    return await _forward(proxy, service_name, path, request)


# One route per method: a single multi-method route would give every method the same
# OpenAPI operation id.
for _method in PROXIED_METHODS:
    router.add_api_route(
        "/{service_name}/{path:path}",
        proxy_path,
        methods=[_method],
        operation_id=f"proxy_{_method.lower()}",
        summary=f"Forward a {_method} request to a service",
        description=_DESCRIPTION,
        responses=_RESPONSES,
        openapi_extra=_ANY_BODY if _method in _METHODS_WITH_BODY else None,
        include_in_schema=_method not in _UNDOCUMENTED_METHODS,
    )


@router.api_route("/{service_name}", methods=PROXIED_METHODS, include_in_schema=False)
async def proxy_root(service_name: str, request: Request, proxy: ProxyServiceDep) -> Response:
    return await _forward(proxy, service_name, "", request)


async def _forward(proxy: ProxyService, service_name: str, path: str, request: Request) -> Response:
    name, pinned, instance_id = service_name.partition(INSTANCE_SEPARATOR)
    inbound = await to_inbound_request(request, path)
    upstream = await proxy.forward(name, inbound, instance_id if pinned else None)
    return to_response(upstream)
