"""Composition root: the only module that knows which concrete classes are used.

Swapping an implementation (another registry, HTTP library or resilience policy)
means changing this file only; the application layer stays untouched.
"""

from dataclasses import dataclass

import httpx

from gateway.application.billing_service import BillingService
from gateway.application.header_policy import HeaderPolicy
from gateway.application.health_service import HealthService
from gateway.application.identity_service import IdentityService
from gateway.application.orchestration import RunOrchestrator
from gateway.application.proxy_service import ProxyService
from gateway.application.x402_service import X402Service
from gateway.config.settings import BillingSettings, Settings
from gateway.domain.billing import Money, default_catalog
from gateway.infrastructure.billing.sqlite_store import SqliteBillingStore
from gateway.infrastructure.billing.stellar import (
    StellarConfig,
    StellarNetwork,
    StellarSignatures,
)
from gateway.infrastructure.httpx_client import HttpxUpstreamClient
from gateway.infrastructure.registry import InMemoryServiceRegistry
from gateway.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerUpstreamClient,
)
from gateway.infrastructure.resilience.load_balancer import LoadBalancingUpstreamClient
from gateway.infrastructure.resilience.retry import RetryingUpstreamClient, RetryPolicy
from gateway.infrastructure.run_log import InMemoryRunLog
from gateway.infrastructure.runs import InMemoryRunStore


@dataclass(frozen=True, slots=True)
class Container:
    proxy_service: ProxyService
    health_service: HealthService
    orchestrator: RunOrchestrator | None = None
    """None when orchestration is off, and then its routes are not registered either. A
    gateway with no agents behind it should not advertise a flow it cannot run."""
    billing: BillingService | None = None
    identity: IdentityService | None = None
    x402: X402Service | None = None
    """All three are None together, or none of them is. Billing without identity would be a
    balance nobody can be shown; identity without billing would be a login that buys nothing.
    x402 is the one that can also be off on its own — it is the machine-facing half, and an
    operator may want only the human one."""
    store: SqliteBillingStore | None = None
    """Held so the ledger's file handle can be closed when the process stops. Nothing else
    reaches for it: every reader goes through ``billing``."""


def build_container(settings: Settings, http_client: httpx.AsyncClient) -> Container:
    registry = InMemoryServiceRegistry(settings.service_definitions())
    transport = HttpxUpstreamClient(http_client)

    retry = settings.retry
    breaker = settings.circuit_breaker
    # Outside in: the balancer picks an instance and fails over to the next one; each instance
    # has its own breaker; the breaker wraps the retries, so one exhausted retry sequence
    # counts as one failure of that instance.
    breakers = CircuitBreakerUpstreamClient(
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

    resilient_client = LoadBalancingUpstreamClient(breakers)
    proxy_service = ProxyService(registry, resilient_client, HeaderPolicy())
    orchestration = settings.orchestration
    billing, identity, x402, store = _billing(settings.billing, http_client)
    return Container(
        proxy_service=proxy_service,
        # Health checks bypass retries and breakers to report the real state.
        health_service=HealthService(registry, transport),
        # The orchestrator shares the proxy, and with it the retries and the breaker: an
        # agent that is failing should not be hammered harder just because the call came
        # from inside the gateway rather than through it.
        orchestrator=(
            RunOrchestrator(
                proxy_service, InMemoryRunStore(), orchestration, InMemoryRunLog(), meter=billing
            )
            if orchestration.enabled
            else None
        ),
        billing=billing,
        identity=identity,
        x402=x402,
        store=store,
    )


def _billing(
    settings: BillingSettings, http_client: httpx.AsyncClient
) -> tuple[
    BillingService | None, IdentityService | None, X402Service | None, SqliteBillingStore | None
]:
    """The billing half of the composition root, or four Nones.

    Kept apart from the rest because it is the only part that touches a disk and the only
    part that can be entirely absent. A gateway with billing off must build exactly the
    container it built before any of this existed — same objects, same behaviour — and a
    single `if` around a block that returns a tuple is easier to be sure of than four
    conditionals threaded through the constructor above.

    The HTTP client is SHARED with the proxy. Horizon gets the same connection pool and the
    same limits as everything else this process talks to, which is what `max_connections`
    is for; a second client would be a second, unbounded one.
    """
    if not settings.enabled:
        return None, None, None, None
    store = SqliteBillingStore(settings.database)
    network = StellarNetwork(
        StellarConfig(
            network=settings.network,
            horizon_url=str(settings.horizon_url) if settings.horizon_url else "",
            destination=settings.destination,
            asset=settings.asset,
            usdc_issuer=settings.usdc_issuer,
            xlm_usd=settings.xlm_usd,
        ),
        http_client,
    )
    billing = BillingService(
        store, network, default_catalog(), settings.asset, settings.reserve_tokens
    )
    identity = IdentityService(StellarSignatures(), settings.secret.get_secret_value())
    x402 = (
        X402Service(billing, network, store, Money.parse(settings.x402_price_usd))
        if settings.x402
        else None
    )
    return billing, identity, x402, store


def default_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=settings.max_connections),
        follow_redirects=False,
    )
