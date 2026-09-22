"""Composition root: the only module that knows which concrete classes are used.

Swapping an implementation (another registry, HTTP library or resilience policy)
means changing this file only; the application layer stays untouched.
"""

from dataclasses import dataclass

import httpx

from gateway.application.header_policy import HeaderPolicy
from gateway.application.health_service import HealthService
from gateway.application.orchestration import RunOrchestrator
from gateway.application.proxy_service import ProxyService
from gateway.config.settings import Settings
from gateway.infrastructure.httpx_client import HttpxUpstreamClient
from gateway.infrastructure.registry import InMemoryServiceRegistry
from gateway.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerUpstreamClient,
)
from gateway.infrastructure.resilience.retry import RetryingUpstreamClient, RetryPolicy
from gateway.infrastructure.runs import InMemoryRunStore


@dataclass(frozen=True, slots=True)
class Container:
    proxy_service: ProxyService
    health_service: HealthService
    orchestrator: RunOrchestrator | None = None
    """None when orchestration is off, and then its routes are not registered either. A
    gateway with no agents behind it should not advertise a flow it cannot run."""


def build_container(settings: Settings, http_client: httpx.AsyncClient) -> Container:
    registry = InMemoryServiceRegistry(settings.service_definitions())
    transport = HttpxUpstreamClient(http_client)

    retry = settings.retry
    breaker = settings.circuit_breaker
    # The breaker wraps the retries: one exhausted retry sequence counts as one failure.
    resilient_client = CircuitBreakerUpstreamClient(
        inner=RetryingUpstreamClient(
            transport,
            RetryPolicy(
                max_attempts=retry.max_attempts,
                base_delay_seconds=retry.base_delay_seconds,
                max_delay_seconds=retry.max_delay_seconds,
                retry_on_status=retry.retry_on_status,
            ),
        ),
        breaker_factory=lambda: CircuitBreaker(
            failure_threshold=breaker.failure_threshold,
            recovery_timeout_seconds=breaker.recovery_timeout_seconds,
        ),
        failure_status_codes=breaker.failure_status_codes,
    )

    proxy_service = ProxyService(registry, resilient_client, HeaderPolicy())
    orchestration = settings.orchestration
    return Container(
        proxy_service=proxy_service,
        # Health checks bypass retries and breakers to report the real state.
        health_service=HealthService(registry, transport),
        # The orchestrator shares the proxy, and with it the retries and the breaker: an
        # agent that is failing should not be hammered harder just because the call came
        # from inside the gateway rather than through it.
        orchestrator=(RunOrchestrator(proxy_service, InMemoryRunStore(), orchestration)
                      if orchestration.enabled else None),
    )


def default_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=settings.max_connections),
        follow_redirects=False,
    )
