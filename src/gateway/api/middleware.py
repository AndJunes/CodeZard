import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from gateway.logging_config import request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# Client-supplied ids end up in logs, so only a safe charset is accepted.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id to every request, echoes it back and logs the outcome."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = self._resolve_request_id(request)
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "%s %s -> %d (%.1f ms)",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
            return response
        finally:
            request_id_var.reset(token)

    @staticmethod
    def _resolve_request_id(request: Request) -> str:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        return incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
