"""Framework-agnostic models shared by every layer."""

import hashlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import quote

from gateway.domain.exceptions import InstanceNotFoundError

Headers = tuple[tuple[str, str], ...]
"""Ordered name/value pairs. A sequence (not a dict) preserves repeated keys like Set-Cookie."""

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Characters allowed unescaped in a URL path segment (RFC 3986 pchar + "/").
_PATH_SAFE_CHARS = "/:@!$&'()*+,;=-._~"


@dataclass(frozen=True, slots=True)
class ServiceInstance:
    """One server running a service. The instances of a service are interchangeable."""

    base_url: str
    id: str = field(init=False)
    """Derived from ``base_url``: stable across restarts and reorderings, and it does not reveal
    the internal address to the clients that see it."""

    def __post_init__(self) -> None:
        digest = hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()
        object.__setattr__(self, "id", digest[:8])


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    """A downstream microservice the gateway can route to."""

    name: str
    base_urls: tuple[str, ...]
    """Every server running the service. Requests are spread among them."""
    timeout_seconds: float = 5.0
    health_path: str = "/health"
    # Sent on every request to the service (e.g. a shared secret). Kept out of repr: they
    # usually hold credentials, and a repr ends up in logs.
    headers: Headers = field(default=(), repr=False)
    instances: tuple[ServiceInstance, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        instances = tuple(ServiceInstance(base_url) for base_url in self.base_urls)
        if not instances:
            raise ValueError(f"Service '{self.name}' needs at least one base URL")
        if len({instance.id for instance in instances}) != len(instances):
            raise ValueError(f"Service '{self.name}' lists the same base URL twice")
        object.__setattr__(self, "instances", instances)

    def instance(self, instance_id: str) -> ServiceInstance:
        for instance in self.instances:
            if instance.id == instance_id:
                return instance
        raise InstanceNotFoundError(self.name, instance_id)


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
    instance: ServiceInstance | None = None
    """The server to send it to. ``None`` until the load balancer picks one, which a service
    with a single instance does not need."""

    @property
    def target(self) -> str:
        """``service@instance`` once an instance is chosen, for keys and logs."""
        return f"{self.service.name}@{self.instance.id}" if self.instance else self.service.name

    @property
    def url(self) -> str:
        # The path arrives already percent-decoded, so it must be re-encoded here.
        base = self._resolved_instance().base_url.rstrip("/")
        path = quote(self.path.lstrip("/"), safe=_PATH_SAFE_CHARS)
        return f"{base}/{path}"

    def _resolved_instance(self) -> ServiceInstance:
        if self.instance is not None:
            return self.instance
        if len(self.service.instances) == 1:
            return self.service.instances[0]
        raise RuntimeError(f"No instance of '{self.service.name}' was chosen for this request")

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
    instance_id: str | None = None
    """The instance that produced it, so a later request can be sent to the same one."""

    async def aclose(self) -> None:
        """Release a streamed body. A no-op for buffered ones."""
        if self.stream is not None:
            await self.stream.aclose()


class HealthStatus(StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    """Some instances of the service are up and some are not."""
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class InstanceHealth:
    id: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    name: str
    instances: tuple[InstanceHealth, ...]

    @property
    def status(self) -> HealthStatus:
        up = sum(instance.status is HealthStatus.UP for instance in self.instances)
        if up == len(self.instances):
            return HealthStatus.UP
        return HealthStatus.DEGRADED if up else HealthStatus.DOWN


@dataclass(frozen=True, slots=True)
class HealthReport:
    services: tuple[ServiceHealth, ...]

    @property
    def is_healthy(self) -> bool:
        return all(service.status is HealthStatus.UP for service in self.services)
