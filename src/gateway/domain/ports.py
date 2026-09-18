"""Abstractions the application layer depends on (Dependency Inversion Principle).

Concrete implementations live in ``gateway.infrastructure`` and are wired in
``gateway.bootstrap``; nothing in the application layer imports them directly.
"""

from abc import ABC, abstractmethod

from gateway.domain.models import OutboundRequest, ServiceDefinition, UpstreamResponse


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
        """Send ``request`` and return the downstream response."""
