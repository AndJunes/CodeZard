"""Maps domain errors to HTTP responses with a consistent JSON body."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from gateway.domain.exceptions import (
    CircuitOpenError,
    GatewayError,
    ServiceNotFoundError,
    UpstreamConnectionError,
    UpstreamTimeoutError,
)

logger = logging.getLogger(__name__)

# New error types only need an entry here (lookup follows the class hierarchy).
_ERROR_STATUS: dict[type[Exception], tuple[int, str]] = {
    ServiceNotFoundError: (status.HTTP_404_NOT_FOUND, "service_not_found"),
    UpstreamConnectionError: (status.HTTP_502_BAD_GATEWAY, "bad_gateway"),
    CircuitOpenError: (status.HTTP_503_SERVICE_UNAVAILABLE, "service_unavailable"),
    UpstreamTimeoutError: (status.HTTP_504_GATEWAY_TIMEOUT, "gateway_timeout"),
    GatewayError: (status.HTTP_500_INTERNAL_SERVER_ERROR, "gateway_error"),
}


def classify(exc: Exception) -> tuple[int, str]:
    for cls in type(exc).__mro__:
        if cls in _ERROR_STATUS:
            return _ERROR_STATUS[cls]
    return status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"


def error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


async def _handle_gateway_error(request: Request, exc: Exception) -> JSONResponse:
    status_code, code = classify(exc)
    level = logging.WARNING if status_code >= 500 else logging.INFO
    logger.log(level, "%s: %s", code, exc)
    return error_response(request, status_code, code, str(exc))


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error", exc_info=exc)
    # Never leak internal details to clients.
    return error_response(
        request, status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "Internal server error"
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(GatewayError, _handle_gateway_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
