"""Logging setup. Every record carries the id of the request being handled."""

import logging
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


def configure_logging(level: str) -> None:
    """Configure the ``gateway`` logger. Safe to call more than once."""
    logger = logging.getLogger("gateway")
    logger.setLevel(level)
    if not any(isinstance(f, RequestIdFilter) for h in logger.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler.addFilter(RequestIdFilter())
        logger.addHandler(handler)
    logger.propagate = False
