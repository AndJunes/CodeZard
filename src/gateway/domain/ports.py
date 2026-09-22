"""Abstractions the application layer depends on (Dependency Inversion Principle).

Concrete implementations live in ``gateway.infrastructure`` and are wired in
``gateway.bootstrap``; nothing in the application layer imports them directly.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from gateway.domain.models import (
    OutboundRequest,
    ServiceDefinition,
    UpstreamResponse,
    UpstreamStream,
)
from gateway.domain.runs import Run


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


class RunStore(ABC):
    """Where runs live between requests.

    It is a port and not a dictionary in the service because the lifetime is a policy: this
    process keeps them in memory and lets them expire, and the PRD forbids carrying them
    between executions. Should that ever change, it changes here and nowhere else.
    """

    @abstractmethod
    async def put(self, run: Run) -> Run:
        """Store ``run`` under its id, replacing any earlier version. Returns what was stored."""

    @abstractmethod
    async def get(self, run_id: str) -> Run:
        """The run, or raise ``RunNotFoundError``. An expired run is a missing one."""


class RunLog(ABC):
    """What a run has already emitted, so a tab that reconnects can catch up.

    A port for the same reason ``RunStore`` is one: how long a log lives, and whether it
    lives anywhere but this process, is a policy — and the PRD's answer today is "in memory,
    bounded, gone on restart". The application layer should not be the place that knows.
    """

    @abstractmethod
    def start(self, run_id: str) -> None:
        """A generation is beginning. Any earlier log for this run is replaced."""

    @abstractmethod
    def append(self, run_id: str, chunk: bytes) -> None:
        """Keep one chunk of the stream."""

    @abstractmethod
    def end(self, run_id: str) -> None:
        """The generation is over, however it ended. Readers stop after draining."""

    @abstractmethod
    def follow(self, run_id: str) -> AsyncIterator[bytes]:
        """Everything said so far, then everything said next, until the run ends."""
