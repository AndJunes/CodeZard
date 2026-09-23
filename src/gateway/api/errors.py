"""Maps domain errors to HTTP responses with a consistent JSON body."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from gateway.api.schemas import ErrorDetail, ErrorResponse
from gateway.application.orchestration import OrchestrationError
from gateway.domain.exceptions import (
    CircuitOpenError,
    GatewayError,
    InstanceNotFoundError,
    RunNotFoundError,
    ServiceNotFoundError,
    UpstreamConnectionError,
    UpstreamTimeoutError,
)
from gateway.domain.runs import IllegalTransitionError

logger = logging.getLogger(__name__)

# New error types only need an entry here (lookup follows the class hierarchy).
_ERROR_STATUS: dict[type[Exception], tuple[int, str]] = {
    ServiceNotFoundError: (status.HTTP_404_NOT_FOUND, "service_not_found"),
    RunNotFoundError: (status.HTTP_404_NOT_FOUND, "run_not_found"),
    # 409 and not 403: the move is not forbidden, it is out of order. Generating before the
    # plan is approved is the case this exists for, and it is now decided by the run's own
    # state instead of by a `status` field in the body the caller sent.
    IllegalTransitionError: (status.HTTP_409_CONFLICT, "illegal_transition"),
    InstanceNotFoundError: (status.HTTP_404_NOT_FOUND, "instance_not_found"),
    UpstreamConnectionError: (status.HTTP_502_BAD_GATEWAY, "bad_gateway"),
    # An agent answered and what it said cannot be used — it asked past the round cap, or
    # returned a plan with nothing in it. Not a 500: nothing here failed, the machine behind
    # us misbehaved, and the caller should be told which.
    OrchestrationError: (status.HTTP_502_BAD_GATEWAY, "upstream_failed"),
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
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=getattr(request.state, "request_id", None),
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


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


async def _handle_bad_request(request: Request, exc: Exception) -> JSONResponse:
    """A `ValueError` from the domain is the caller's fault, not the server's.

    `Run.start` raises one for an empty idea and for an idea past FR-1.1's 4,000 characters.
    Without this they reached the catch-all and came back as 500 "Internal server error",
    which tells whoever typed too much nothing about what to do.
    """
    logger.info("bad_request: %s", exc)
    return error_response(request, status.HTTP_400_BAD_REQUEST, "bad_request", str(exc))


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ValueError, _handle_bad_request)
    app.add_exception_handler(GatewayError, _handle_gateway_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
