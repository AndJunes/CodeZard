"""Composition root, entry point, logging and error classification."""

import logging
from typing import Any

import httpx
import pytest

from gateway import main
from gateway.api.errors import classify
from gateway.application.health_service import HealthService
from gateway.application.proxy_service import ProxyService
from gateway.bootstrap import build_container, default_http_client
from gateway.config.settings import ServiceSettings, Settings
from gateway.domain.exceptions import (
    CircuitOpenError,
    DuplicateServiceError,
    ServiceNotFoundError,
    UpstreamConnectionError,
    UpstreamTimeoutError,
)
from gateway.logging_config import RequestIdFilter, configure_logging, request_id_var


async def test_build_container_wires_the_use_cases() -> None:
    settings = Settings(
        _env_file=None, services=[ServiceSettings(name="users", base_url="http://users:8001")]
    )

    async with httpx.AsyncClient() as http:
        container = build_container(settings, http)

    assert isinstance(container.proxy_service, ProxyService)
    assert isinstance(container.health_service, HealthService)


async def test_default_http_client_does_not_follow_redirects() -> None:
    async with default_http_client(Settings(_env_file=None)) as http:
        assert http.follow_redirects is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ServiceNotFoundError("x"), (404, "service_not_found")),
        (UpstreamConnectionError("x"), (502, "bad_gateway")),
        (CircuitOpenError("x"), (503, "service_unavailable")),
        (UpstreamTimeoutError("x"), (504, "gateway_timeout")),
        (DuplicateServiceError("x"), (500, "gateway_error")),
        (RuntimeError("x"), (500, "internal_error")),
    ],
)
def test_classify_maps_errors_to_http_status(error: Exception, expected: tuple[int, str]) -> None:
    assert classify(error) == expected


def test_run_starts_uvicorn_with_the_app_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        main.uvicorn, "run", lambda *args, **kwargs: calls.update(args=args, **kwargs)
    )
    monkeypatch.setattr(
        main, "get_settings", lambda: Settings(_env_file=None, port=9000, log_level="DEBUG")
    )

    main.run()

    assert calls == {
        "args": ("gateway.api.app:create_app",),
        "factory": True,
        "host": "0.0.0.0",
        "port": 9000,
        "log_level": "debug",
    }


def test_configure_logging_is_idempotent() -> None:
    configure_logging("DEBUG")
    configure_logging("INFO")

    logger = logging.getLogger("gateway")
    # pytest attaches its own capture handlers, so count only the gateway's.
    gateway_handlers = [
        h for h in logger.handlers if any(isinstance(f, RequestIdFilter) for f in h.filters)
    ]
    assert len(gateway_handlers) == 1
    assert logger.level == logging.INFO


def test_log_records_carry_the_current_request_id() -> None:
    record = logging.LogRecord("gateway", logging.INFO, __file__, 1, "msg", None, None)
    log_filter = RequestIdFilter()

    log_filter.filter(record)
    assert record.request_id == "-"

    token = request_id_var.set("rid-1")
    try:
        log_filter.filter(record)
    finally:
        request_id_var.reset(token)
    assert record.request_id == "rid-1"
