"""Entry point: ``gateway`` (console script) or ``python -m gateway``."""

import uvicorn

from gateway.config.settings import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "gateway.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
