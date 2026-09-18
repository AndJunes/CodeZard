import logging
import time
from collections.abc import Callable
from enum import StrEnum

from gateway.domain.exceptions import CircuitOpenError, UpstreamError
from gateway.domain.models import OutboundRequest, UpstreamResponse
from gateway.domain.ports import UpstreamClient

logger = logging.getLogger(__name__)

Clock = Callable[[], float]


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Classic three-state circuit breaker.

    CLOSED    -> requests flow; ``failure_threshold`` consecutive failures open it.
    OPEN      -> requests are rejected until ``recovery_timeout_seconds`` elapse.
    HALF_OPEN -> a single probe request is let through; its outcome closes or reopens it.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        clock: Clock = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def try_acquire(self) -> bool:
        """Return whether a request may proceed right now."""
        if self._state is CircuitState.OPEN:
            if self._clock() - self._opened_at < self._recovery_timeout:
                return False
            self._state = CircuitState.HALF_OPEN
        if self._state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
        return True

    def on_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False
        self._failures = 0

    def on_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
        elif self._state is CircuitState.CLOSED:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._trip()

    def on_abort(self) -> None:
        """The call ended without a verdict (e.g. cancelled): free the probe slot."""
        self._probe_in_flight = False

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._failures = 0
        self._probe_in_flight = False


class CircuitBreakerUpstreamClient(UpstreamClient):
    """Keeps one ``CircuitBreaker`` per service so a failing service cannot drag down the rest."""

    def __init__(
        self,
        inner: UpstreamClient,
        breaker_factory: Callable[[], CircuitBreaker],
        failure_status_codes: frozenset[int] = frozenset({502, 503, 504}),
    ) -> None:
        self._inner = inner
        self._breaker_factory = breaker_factory
        self._failure_status_codes = failure_status_codes
        self._breakers: dict[str, CircuitBreaker] = {}

    def state_of(self, service_name: str) -> CircuitState:
        return self._breaker_for(service_name).state

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        service_name = request.service.name
        breaker = self._breaker_for(service_name)
        if not breaker.try_acquire():
            raise CircuitOpenError(service_name)

        previous_state = breaker.state
        try:
            response = await self._inner.send(request)
        except UpstreamError:
            breaker.on_failure()
            self._log_transition(service_name, previous_state, breaker.state)
            raise
        except BaseException:
            breaker.on_abort()
            raise

        if response.status_code in self._failure_status_codes:
            breaker.on_failure()
        else:
            breaker.on_success()
        self._log_transition(service_name, previous_state, breaker.state)
        return response

    def _breaker_for(self, service_name: str) -> CircuitBreaker:
        if service_name not in self._breakers:
            self._breakers[service_name] = self._breaker_factory()
        return self._breakers[service_name]

    @staticmethod
    def _log_transition(service_name: str, before: CircuitState, after: CircuitState) -> None:
        if before is not after:
            logger.warning("Circuit for '%s' changed: %s -> %s", service_name, before, after)
