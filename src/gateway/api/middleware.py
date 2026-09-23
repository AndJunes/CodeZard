import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.logging_config import request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# Client-supplied ids end up in logs, so only a safe charset is accepted.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class RequestContextMiddleware:
    """Assigns a request id to every request, echoes it back and logs the outcome.

    Written as pure ASGI rather than ``BaseHTTPMiddleware``, and the reason is streaming.
    ``BaseHTTPMiddleware`` returns from ``call_next`` as soon as ``http.response.start``
    arrives, before the body, which made three things wrong at once:

    * the duration logged below was the time to the first byte, not the real one — on a
      stream that runs for minutes the difference is the whole point;
    * ``request_id_var`` was reset before the body finished, so every log line emitted
      while chunks were being pumped lost its request id;
    * background tasks — the one that returns the upstream connection to the pool — ran
      against the last bytes still in flight (starlette#3458), truncating the stream.

    Here the timer stops and the context is released on the final ``http.response.body``,
    which is when the response has actually been sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        request_id = self._resolve_request_id(request)
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 0
        finished = False

        async def send_with_context(message: Message) -> None:
            nonlocal status_code, finished
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            await send(message)
            if finished:
                logger.info(
                    "%s %s -> %d (%.1f ms)",
                    request.method,
                    request.url.path,
                    status_code,
                    (time.perf_counter() - started) * 1000,
                )

        try:
            await self._app(scope, receive, send_with_context)
        finally:
            request_id_var.reset(token)

    @staticmethod
    def _resolve_request_id(request: Request) -> str:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        return incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
