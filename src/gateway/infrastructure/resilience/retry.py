import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamTimeoutError
from gateway.domain.models import OutboundRequest, UpstreamResponse, UpstreamStream
from gateway.domain.ports import UpstreamClient

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

_RETRYABLE_ERRORS = (UpstreamConnectionError, UpstreamTimeoutError)

# Both carry a status code, which is all the retry decision looks at.
_Outcome = TypeVar("_Outcome", UpstreamResponse, UpstreamStream)


async def _discard_response(response: UpstreamResponse) -> None:
    """Release a discarded response in case it owns a streamed body."""
    await response.aclose()


async def _discard_stream(stream: UpstreamStream) -> None:
    """A stream holds an open connection, so dropping it has to close it.

    Without this every retried attempt would leak a connection from the pool. The loop
    below was written when discarding was free, and that assumption is only true for
    ``UpstreamResponse``.
    """
    await stream.aclose()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff: ``base * 2**(attempt - 1)``, capped at ``max_delay``."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.1
    max_delay_seconds: float = 2.0
    retry_on_status: frozenset[int] = frozenset({502, 503, 504})

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays cannot be negative")

    def delay_for(self, attempt: int) -> float:
        return min(self.base_delay_seconds * 2.0 ** (attempt - 1), self.max_delay_seconds)

    def should_retry_status(self, status_code: int) -> bool:
        return status_code in self.retry_on_status


class RetryingUpstreamClient(UpstreamClient):
    """Retries idempotent requests on transient failures.

    Non-idempotent methods (POST, PATCH) are never retried: repeating them could
    duplicate side effects in the downstream service.
    """

    def __init__(
        self,
        inner: UpstreamClient,
        policy: RetryPolicy,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._sleep = sleep

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        return await self._attempts(request, self._inner.send, _discard_response)

    async def stream(self, request: OutboundRequest) -> UpstreamStream:
        """Same policy, and it still works: the status arrives with the headers.

        What changes is the end of the road. Once the stream is handed back, the caller
        starts writing bytes to the client, so there is no retry left to make: the client
        already has a status code and headers. Every retry happens strictly before that.
        """
        return await self._attempts(request, self._inner.stream, _discard_stream)

    async def _attempts(
        self,
        request: OutboundRequest,
        call: Callable[[OutboundRequest], Awaitable[_Outcome]],
        discard: Callable[[_Outcome], Awaitable[None]],
    ) -> _Outcome:
        if not request.is_idempotent:
            return await call(request)

        attempt = 1
        while True:
            try:
                outcome = await call(request)
            except _RETRYABLE_ERRORS as exc:
                if attempt >= self._policy.max_attempts:
                    raise
                reason = type(exc).__name__
            else:
                if attempt >= self._policy.max_attempts or not self._policy.should_retry_status(
                    outcome.status_code
                ):
                    return outcome
                reason = f"status {outcome.status_code}"
                await discard(outcome)

            delay = self._policy.delay_for(attempt)
            logger.warning(
                "Retrying %s %s after %s (attempt %d/%d, waiting %.2fs)",
                request.method,
                request.url,
                reason,
                attempt,
                self._policy.max_attempts,
                delay,
            )
            await self._sleep(delay)
            attempt += 1
