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
from gateway.api.routes import billing, health, proxy, runs, x402
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
            container = build_container(settings, http_client)
            app.state.container = container
            logger.info("Gateway ready. Registered services: %s", _describe_services(settings))
            if container.billing is not None:
                logger.info(
                    "Billing is on: %s, paying to %s, ledger at %s",
                    settings.billing.network,
                    settings.billing.destination[:8] + "…",
                    settings.billing.database,
                )
            try:
                yield
            finally:
                # The ledger holds a file handle and a WAL. Closing it is the difference
                # between a clean shutdown and one that leaves `-wal` files behind for the
                # next start to recover from.
                if container.store is not None:
                    container.store.close()

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
    # BEFORE the proxy, always. The proxy is a catch-all under `/api`, and FastAPI matches in
    # registration order: a router included after it is only reachable at a path the
    # catch-all cannot spell. `/runs` avoids the collision; this ordering makes it moot.
    if settings.orchestration.enabled:
        app.include_router(runs.router)
        if not (settings.orchestration.pm_token and settings.orchestration.backend_token):
            logger.warning(
                "Orchestration is on and at least one agent token is empty: "
                "the agents behind this gateway are open to whoever reaches them."
            )
    # Same rule as `/runs`: before the catch-all, and only when there is something behind it.
    # A gateway that sells nothing should not answer a checkout, and one that cannot verify a
    # signature should not offer a sign-in that would have to fail.
    if settings.billing.enabled:
        app.include_router(billing.router)
        if settings.billing.x402:
            app.include_router(x402.router)
        if settings.billing.network == "public":
            logger.warning("Billing is pointed at Stellar MAINNET: payments move real money.")
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
