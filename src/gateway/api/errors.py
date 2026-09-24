"""Maps domain errors to HTTP responses with a consistent JSON body."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from gateway.api.payment_gate import PaymentRequiredError
from gateway.api.schemas import ErrorDetail, ErrorResponse
from gateway.application.identity_service import IdentityError, UnverifiableError
from gateway.application.orchestration import OrchestrationError
from gateway.domain.billing import (
    BillingError,
    InsufficientFundsError,
    InvoiceNotFoundError,
)
from gateway.domain.exceptions import (
    CircuitOpenError,
    ConsoleDisabledError,
    GatewayError,
    InstanceNotFoundError,
    NoProjectError,
    ProjectGoneError,
    RunNotFoundError,
    ServiceNotFoundError,
    UpstreamConnectionError,
    UpstreamTimeoutError,
)
from gateway.domain.runs import IllegalTransitionError
from gateway.domain.x402 import X402Error

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
    # 404: an off switch reads as a route that is not there, which is what it is.
    ConsoleDisabledError: (status.HTTP_404_NOT_FOUND, "console_disabled"),
    NoProjectError: (status.HTTP_409_CONFLICT, "no_project"),
    # 410, like the agent's own: the thing existed and is gone, and asking again will not help.
    ProjectGoneError: (status.HTTP_410_GONE, "project_gone"),
    UpstreamConnectionError: (status.HTTP_502_BAD_GATEWAY, "bad_gateway"),
    # An agent answered and what it said cannot be used — it asked past the round cap, or
    # returned a plan with nothing in it. Not a 500: nothing here failed, the machine behind
    # us misbehaved, and the caller should be told which.
    OrchestrationError: (status.HTTP_502_BAD_GATEWAY, "upstream_failed"),
    CircuitOpenError: (status.HTTP_503_SERVICE_UNAVAILABLE, "service_unavailable"),
    UpstreamTimeoutError: (status.HTTP_504_GATEWAY_TIMEOUT, "gateway_timeout"),
    # 402, and it is not an error the caller did anything wrong to earn: they have not paid
    # yet, and the body says what it would cost. `PaymentRequiredError` never reaches here —
    # it has its own handler, because its body is the protocol's shape and not ours.
    InsufficientFundsError: (status.HTTP_402_PAYMENT_REQUIRED, "insufficient_funds"),
    # Same reasoning as `RunNotFoundError`: an invoice id is the only thing protecting an
    # invoice, so "it expired" and "it never existed" are deliberately one answer.
    InvoiceNotFoundError: (status.HTTP_404_NOT_FOUND, "invoice_not_found"),
    BillingError: (status.HTTP_400_BAD_REQUEST, "billing_error"),
    # The ordinary route for one of these is `payment_gate`, which catches it and answers
    # 402 with the price. This is the net for one that escapes somewhere else.
    X402Error: (status.HTTP_400_BAD_REQUEST, "invalid_payment"),
    # 501 and not 401: this deployment cannot check signatures at all, the caller did nothing
    # wrong, and retrying will not help. Above the `IdentityError` it derives from.
    UnverifiableError: (status.HTTP_501_NOT_IMPLEMENTED, "not_implemented"),
    IdentityError: (status.HTTP_401_UNAUTHORIZED, "unauthorized"),
    GatewayError: (status.HTTP_500_INTERNAL_SERVER_ERROR, "gateway_error"),
}

_AUTHENTICATE = {status.HTTP_401_UNAUTHORIZED: {"WWW-Authenticate": "Bearer"}}
"""A 401 without this header is a 401 that does not say how to fix itself."""


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
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
        headers=_AUTHENTICATE.get(status_code),
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


async def _handle_bad_request(request: Request, exc: Exception) -> JSONResponse:
    """A `ValueError` from the domain is the caller's fault, not the server's.

    `Run.start` raises one for an empty idea and for an idea past FR-1.1's 4,000 characters.
    Without this they reached the catch-all and came back as 500 "Internal server error",
    which tells whoever typed too much nothing about what to do.
    """
    logger.info("bad_request: %s", exc)
    return error_response(request, status.HTTP_400_BAD_REQUEST, "bad_request", str(exc))


async def _handle_payment_required(request: Request, exc: Exception) -> JSONResponse:
    """The one response in this gateway that is NOT wrapped in the house error envelope.

    x402 specifies the 402 body exactly — ``{x402Version, error, accepts: [...]}`` — and an
    off-the-shelf client reads that and nothing else. Putting it inside ``{"error": {...}}``
    would make this gateway unpayable by every tool that already speaks the protocol, which
    is the entire reason for speaking it.
    """
    assert isinstance(exc, PaymentRequiredError)
    logger.info("payment_required: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_402_PAYMENT_REQUIRED, content=exc.document.as_json()
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ValueError, _handle_bad_request)
    # BEFORE the GatewayError handler it derives from: FastAPI matches the most specific
    # registered class, and this one must not be flattened into the generic envelope.
    app.add_exception_handler(PaymentRequiredError, _handle_payment_required)
    app.add_exception_handler(GatewayError, _handle_gateway_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
