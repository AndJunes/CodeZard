import asyncio
from typing import Any

import pytest

from gateway.domain.exceptions import CircuitOpenError, UpstreamConnectionError
from gateway.domain.models import ServiceDefinition, UpstreamResponse
from gateway.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerUpstreamClient,
    CircuitState,
)
from tests.fakes import FakeClock, ScriptedUpstreamClient, make_request

RECOVERY_SECONDS = 10.0
OK = UpstreamResponse(status_code=200)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=3, recovery_timeout_seconds=RECOVERY_SECONDS, clock=clock
    )


def trip(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        breaker.on_failure()


class TestCircuitBreaker:
    def test_starts_closed_and_allows_requests(self, breaker: CircuitBreaker) -> None:
        assert breaker.state is CircuitState.CLOSED
        assert breaker.try_acquire()

    def test_opens_after_consecutive_failures(self, breaker: CircuitBreaker) -> None:
        breaker.on_failure()
        breaker.on_failure()
        assert breaker.state is CircuitState.CLOSED

        breaker.on_failure()

        assert breaker.state is CircuitState.OPEN
        assert not breaker.try_acquire()

    def test_success_resets_the_failure_count(self, breaker: CircuitBreaker) -> None:
        breaker.on_failure()
        breaker.on_failure()
        breaker.on_success()
        breaker.on_failure()
        breaker.on_failure()

        assert breaker.state is CircuitState.CLOSED

    def test_allows_a_single_probe_after_recovery_timeout(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        trip(breaker)

        clock.advance(RECOVERY_SECONDS - 0.1)
        assert not breaker.try_acquire()

        clock.advance(0.1)
        assert breaker.try_acquire()
        assert breaker.state is CircuitState.HALF_OPEN
        assert not breaker.try_acquire()

    def test_successful_probe_closes_the_circuit(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        trip(breaker)
        clock.advance(RECOVERY_SECONDS)
        breaker.try_acquire()

        breaker.on_success()

        assert breaker.state is CircuitState.CLOSED
        assert breaker.try_acquire()

    def test_failed_probe_reopens_and_restarts_the_timeout(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        trip(breaker)
        clock.advance(RECOVERY_SECONDS)
        breaker.try_acquire()

        breaker.on_failure()

        assert breaker.state is CircuitState.OPEN
        clock.advance(RECOVERY_SECONDS - 1)
        assert not breaker.try_acquire()
        clock.advance(1)
        assert breaker.try_acquire()

    def test_aborted_probe_frees_the_probe_slot(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        trip(breaker)
        clock.advance(RECOVERY_SECONDS)
        breaker.try_acquire()

        breaker.on_abort()

        assert breaker.try_acquire()

    def test_late_failures_while_open_do_not_extend_the_timeout(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        trip(breaker)
        clock.advance(RECOVERY_SECONDS / 2)

        breaker.on_failure()

        clock.advance(RECOVERY_SECONDS / 2)
        assert breaker.try_acquire()

    @pytest.mark.parametrize(
        "kwargs",
        [{"failure_threshold": 0}, {"recovery_timeout_seconds": 0}],
    )
    def test_rejects_invalid_configuration(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="must be"):
            CircuitBreaker(**kwargs)


def breaker_client(inner: ScriptedUpstreamClient, clock: FakeClock) -> CircuitBreakerUpstreamClient:
    return CircuitBreakerUpstreamClient(
        inner,
        breaker_factory=lambda: CircuitBreaker(
            failure_threshold=2, recovery_timeout_seconds=RECOVERY_SECONDS, clock=clock
        ),
    )


class TestCircuitBreakerUpstreamClient:
    async def test_rejects_calls_while_open_without_reaching_the_service(
        self, users_service: ServiceDefinition, clock: FakeClock
    ) -> None:
        inner = ScriptedUpstreamClient(UpstreamConnectionError("users"))
        client = breaker_client(inner, clock)

        for _ in range(2):
            with pytest.raises(UpstreamConnectionError):
                await client.send(make_request(users_service))
        with pytest.raises(CircuitOpenError, match="'users'"):
            await client.send(make_request(users_service))

        assert inner.calls == 2
        assert client.state_of("users") is CircuitState.OPEN

    async def test_failure_status_codes_count_as_failures(
        self, users_service: ServiceDefinition, clock: FakeClock
    ) -> None:
        inner = ScriptedUpstreamClient(UpstreamResponse(status_code=503))
        client = breaker_client(inner, clock)

        for _ in range(2):
            assert (await client.send(make_request(users_service))).status_code == 503
        with pytest.raises(CircuitOpenError):
            await client.send(make_request(users_service))

    @pytest.mark.parametrize("status_code", [200, 404, 500])
    async def test_other_status_codes_keep_the_circuit_closed(
        self, users_service: ServiceDefinition, clock: FakeClock, status_code: int
    ) -> None:
        client = breaker_client(
            ScriptedUpstreamClient(UpstreamResponse(status_code=status_code)), clock
        )

        for _ in range(5):
            await client.send(make_request(users_service))

        assert client.state_of("users") is CircuitState.CLOSED

    async def test_breakers_are_isolated_per_service(
        self,
        users_service: ServiceDefinition,
        orders_service: ServiceDefinition,
        clock: FakeClock,
    ) -> None:
        inner = ScriptedUpstreamClient(
            UpstreamConnectionError("users"), UpstreamConnectionError("users"), OK
        )
        client = breaker_client(inner, clock)
        for _ in range(2):
            with pytest.raises(UpstreamConnectionError):
                await client.send(make_request(users_service))

        response = await client.send(make_request(orders_service))

        assert response is OK
        assert client.state_of("users") is CircuitState.OPEN
        assert client.state_of("orders") is CircuitState.CLOSED

    async def test_recovers_once_the_service_is_back(
        self, users_service: ServiceDefinition, clock: FakeClock
    ) -> None:
        inner = ScriptedUpstreamClient(
            UpstreamConnectionError("users"), UpstreamConnectionError("users"), OK
        )
        client = breaker_client(inner, clock)
        for _ in range(2):
            with pytest.raises(UpstreamConnectionError):
                await client.send(make_request(users_service))

        clock.advance(RECOVERY_SECONDS)
        response = await client.send(make_request(users_service))

        assert response is OK
        assert client.state_of("users") is CircuitState.CLOSED

    async def test_cancelled_probe_does_not_block_the_next_one(
        self, users_service: ServiceDefinition, clock: FakeClock
    ) -> None:
        inner = ScriptedUpstreamClient(
            UpstreamConnectionError("users"),
            UpstreamConnectionError("users"),
            asyncio.CancelledError(),
            OK,
        )
        client = breaker_client(inner, clock)
        for _ in range(2):
            with pytest.raises(UpstreamConnectionError):
                await client.send(make_request(users_service))
        clock.advance(RECOVERY_SECONDS)

        with pytest.raises(asyncio.CancelledError):
            await client.send(make_request(users_service))
        response = await client.send(make_request(users_service))

        assert response is OK
