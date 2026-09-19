import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamTimeoutError
from gateway.domain.models import OutboundRequest, UpstreamResponse
from gateway.domain.ports import UpstreamClient

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

_RETRYABLE_ERRORS = (UpstreamConnectionError, UpstreamTimeoutError)


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
        if not request.is_idempotent:
            return await self._inner.send(request)

        attempt = 1
        while True:
            try:
                response = await self._inner.send(request)
            except _RETRYABLE_ERRORS as exc:
                if attempt >= self._policy.max_attempts:
                    raise
                reason = type(exc).__name__
            else:
                if attempt >= self._policy.max_attempts or not self._policy.should_retry_status(
                    response.status_code
                ):
                    return response
                await response.aclose()  # discarded: free its connection before trying again
                reason = f"status {response.status_code}"

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
