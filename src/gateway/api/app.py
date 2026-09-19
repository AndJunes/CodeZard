"""Application factory."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from gateway import __version__
from gateway.api.contracts import CONTRACTS
from gateway.api.errors import register_error_handlers
from gateway.api.middleware import RequestContextMiddleware
from gateway.api.openapi import API_DESCRIPTION, TAGS, install_openapi
from gateway.api.routes import health, proxy
from gateway.bootstrap import build_container, default_http_client
from gateway.config.settings import Settings, get_settings
from gateway.logging_config import configure_logging

logger = logging.getLogger(__name__)

HttpClientFactory = Callable[[Settings], httpx.AsyncClient]


def create_app(
    settings: Settings | None = None,
    http_client_factory: HttpClientFactory = default_http_client,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with http_client_factory(settings) as http_client:
            app.state.container = build_container(settings, http_client)
            logger.info("Gateway ready. Registered services: %s", _describe_services(settings))
            yield

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(proxy.router)
    install_openapi(app, (service.name for service in settings.services), CONTRACTS)
    return app


def _describe_services(settings: Settings) -> str:
    """E.g. ``mirag (3fa1c2d0 http://mirag-1:8000, 9b2e4d11 http://mirag-2:8000)``: the log is
    where an instance id seen by a client can be matched to its server."""
    described = [
        f"{service.name} ({', '.join(f'{i.id} {i.base_url}' for i in service.instances)})"
        for service in settings.service_definitions()
    ]
    return ", ".join(described) or "none"
