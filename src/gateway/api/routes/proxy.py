"""Catch-all routes: ``/api/{service}/{path}`` is forwarded to ``{service_base_url}/{path}``."""

from fastapi import APIRouter, Request, Response

from gateway.api.adapters import to_inbound_request, to_response
from gateway.api.dependencies import ProxyServiceDep
from gateway.application.proxy_service import ProxyService

PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Excluded from OpenAPI: a catch-all route has no meaningful schema to document.
router = APIRouter(prefix="/api", tags=["proxy"], include_in_schema=False)


@router.api_route("/{service_name}/{path:path}", methods=PROXIED_METHODS)
async def proxy_path(
    service_name: str, path: str, request: Request, proxy: ProxyServiceDep
) -> Response:
    return await _forward(proxy, service_name, path, request)


@router.api_route("/{service_name}", methods=PROXIED_METHODS)
async def proxy_root(service_name: str, request: Request, proxy: ProxyServiceDep) -> Response:
    return await _forward(proxy, service_name, "", request)


async def _forward(proxy: ProxyService, service_name: str, path: str, request: Request) -> Response:
    inbound = await to_inbound_request(request, path)
    upstream = await proxy.forward(service_name, inbound)
    return to_response(upstream)
