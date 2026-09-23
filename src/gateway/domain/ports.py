"""Abstractions the application layer depends on (Dependency Inversion Principle).

Concrete implementations live in ``gateway.infrastructure`` and are wired in
``gateway.bootstrap``; nothing in the application layer imports them directly.
"""

from abc import ABC, abstractmethod

from gateway.domain.models import (
    OutboundRequest,
    ServiceDefinition,
    UpstreamResponse,
    UpstreamStream,
)


class ServiceRegistry(ABC):
    """Resolves service names to their definitions."""

    @abstractmethod
    def get(self, name: str) -> ServiceDefinition:
        """Return the service called ``name`` or raise ``ServiceNotFoundError``."""

    @abstractmethod
    def all(self) -> list[ServiceDefinition]:
        """Return every registered service."""


class UpstreamClient(ABC):
    """Sends requests to downstream services.

    Implementations must translate transport failures into ``UpstreamError``
    subclasses so callers never depend on a specific HTTP library.
    """

    @abstractmethod
    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        """Send ``request``, read the whole body, and return the downstream response."""

    @abstractmethod
    async def stream(self, request: OutboundRequest) -> UpstreamStream:
        """Send ``request`` and return as soon as the status and headers arrive.

        The body is left unread on an open connection, so the guarantee above only covers
        the exchange up to the headers: a failure while the caller consumes ``chunks``
        surfaces as whatever the transport raises, not as an ``UpstreamError``. By then the
        client already holds a status code, so there is nothing left to translate it into.

        The caller owns the returned stream and must await ``aclose``.
        """
