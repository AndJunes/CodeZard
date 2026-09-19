"""Framework-agnostic models shared by every layer."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
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
    # Sent on every request to the service (e.g. a shared secret). Kept out of repr: they
    # usually hold credentials, and a repr ends up in logs.
    headers: Headers = field(default=(), repr=False)


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


class ByteStream(ABC):
    """A response body relayed chunk by chunk as it arrives, instead of all at once.

    Whoever ends up holding it must call ``aclose``, even without iterating it, so the
    connection behind it is released. Like ``UpstreamClient``, implementations raise
    ``UpstreamError`` subclasses, never transport-specific exceptions.
    """

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the body chunks in order."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the connection. Safe to call more than once."""


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    """The response produced by a downstream service."""

    status_code: int
    headers: Headers = ()
    body: bytes = b""
    stream: ByteStream | None = None
    """Set instead of ``body`` when the response must reach the client as it is produced."""

    async def aclose(self) -> None:
        """Release a streamed body. A no-op for buffered ones."""
        if self.stream is not None:
            await self.stream.aclose()


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
