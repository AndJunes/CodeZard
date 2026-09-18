"""Framework-agnostic models shared by every layer."""

from dataclasses import dataclass
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
