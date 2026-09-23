"""Test doubles. They implement the same ports as production code (Liskov substitution)."""

from collections import deque
from collections.abc import AsyncIterator

from gateway.domain.models import (
    OutboundRequest,
    ServiceDefinition,
    UpstreamResponse,
    UpstreamStream,
)
from gateway.domain.ports import UpstreamClient

Outcome = UpstreamResponse | BaseException


class ScriptedUpstreamClient(UpstreamClient):
    """Returns (or raises) the scripted outcomes in order; the last one repeats forever."""

    def __init__(self, *outcomes: Outcome) -> None:
        if not outcomes:
            raise ValueError("at least one outcome is required")
        self._outcomes = deque(outcomes)
        self.requests: list[OutboundRequest] = []
        self.closed = 0

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        return self._next(request)

    async def stream(self, request: OutboundRequest) -> UpstreamStream:
        """The same script, handed over as a stream.

        ``closed`` counts how many streams were closed, which is what proves the retry
        decorator does not leak the attempts it throws away.
        """
        response = self._next(request)

        async def chunks() -> AsyncIterator[bytes]:
            yield response.body

        async def aclose() -> None:
            self.closed += 1

        return UpstreamStream(
            status_code=response.status_code,
            headers=response.headers,
            chunks=chunks(),
            aclose=aclose,
        )

    def _next(self, request: OutboundRequest) -> UpstreamResponse:
        self.requests.append(request)
        outcome = self._outcomes.popleft() if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Replaces ``asyncio.sleep`` so backoff can be asserted without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_request(
    service: ServiceDefinition, method: str = "GET", path: str = "/items"
) -> OutboundRequest:
    return OutboundRequest(service=service, method=method, path=path)
