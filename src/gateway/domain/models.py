"""Framework-agnostic models shared by every layer."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import quote

Headers = tuple[tuple[str, str], ...]
"""Ordered name/value pairs. A sequence (not a dict) preserves repeated keys like Set-Cookie."""

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Characters allowed unescaped in a URL path segment (RFC 3986 pchar + "/").
_PATH_SAFE_CHARS = "/:@!$&'()*+,;=-._~"


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    """A downstream microservice the gateway can route to."""

    name: str
    base_url: str
    timeout_seconds: float = 5.0
    health_path: str = "/health"
    read_timeout_seconds: float | None = None
    """Seconds allowed between two chunks; falls back to ``timeout_seconds``.

    It exists because a streamed response changes what "read timeout" means. On a buffered
    response it bounds the whole download; on a stream it bounds the *gap* between chunks.
    A service that pushes events for ten minutes needs a long gap and a short connect, and
    a single scalar cannot say both.
    """

    @property
    def read_timeout(self) -> float:
        if self.read_timeout_seconds is None:
            return self.timeout_seconds
        return self.read_timeout_seconds


@dataclass(frozen=True, slots=True)
class InboundRequest:
    """A client request as received by the gateway, free of framework types."""

    method: str
    path: str
    headers: Headers = ()
    query_params: Headers = ()
    body: bytes = b""
    client_host: str | None = None
    scheme: str = "http"
    host: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    """A request the gateway sends to a downstream service."""

    service: ServiceDefinition
    method: str
    path: str
    headers: Headers = ()
    query_params: Headers = ()
    body: bytes = b""

    @property
    def url(self) -> str:
        # The path arrives already percent-decoded, so it must be re-encoded here.
        base = self.service.base_url.rstrip("/")
        path = quote(self.path.lstrip("/"), safe=_PATH_SAFE_CHARS)
        return f"{base}/{path}"

    @property
    def is_idempotent(self) -> bool:
        return self.method.upper() in IDEMPOTENT_METHODS


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    """The response produced by a downstream service."""

    status_code: int
    headers: Headers = ()
    body: bytes = b""


async def _no_chunks() -> AsyncIterator[bytes]:
    return
    yield b""  # pragma: no cover - makes the function an async generator


async def _noop() -> None:
    return


@dataclass(frozen=True, slots=True)
class UpstreamStream:
    """A downstream response whose body has not been read yet.

    Unlike ``UpstreamResponse`` this is **not** a self-contained value: it holds a live
    connection. Whoever receives one owns it and must await ``aclose`` exactly once, even
    if ``chunks`` is never consumed. Forgetting leaks a connection from the pool.
    """

    status_code: int
    headers: Headers = ()
    chunks: AsyncIterator[bytes] = field(default_factory=_no_chunks)
    aclose: Callable[[], Awaitable[None]] = _noop


class HealthStatus(StrEnum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    services: tuple[ServiceHealth, ...]

    @property
    def is_healthy(self) -> bool:
        return all(service.status is HealthStatus.UP for service in self.services)
