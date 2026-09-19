from typing import Any

import pytest

from gateway.domain.exceptions import UpstreamConnectionError, UpstreamTimeoutError
from gateway.domain.models import ServiceDefinition, UpstreamResponse
from gateway.infrastructure.resilience.retry import RetryingUpstreamClient, RetryPolicy
from tests.fakes import FakeByteStream, RecordingSleep, ScriptedUpstreamClient, make_request

OK = UpstreamResponse(status_code=200, body=b"ok")
UNAVAILABLE = UpstreamResponse(status_code=503)


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


def retrying(
    inner: ScriptedUpstreamClient, sleep: RecordingSleep, **policy: Any
) -> RetryingUpstreamClient:
    return RetryingUpstreamClient(inner, RetryPolicy(**policy), sleep=sleep)


async def test_returns_first_success_without_retrying(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(OK)

    response = await retrying(inner, sleep).send(make_request(users_service))

    assert response is OK
    assert inner.calls == 1
    assert sleep.delays == []


async def test_retries_transport_errors_with_exponential_backoff(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(
        UpstreamConnectionError("users"), UpstreamTimeoutError("users"), OK
    )

    response = await retrying(inner, sleep, base_delay_seconds=0.1).send(
        make_request(users_service)
    )

    assert response is OK
    assert inner.calls == 3
    assert sleep.delays == [0.1, 0.2]


async def test_raises_last_error_when_attempts_are_exhausted(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(UpstreamConnectionError("users"))

    with pytest.raises(UpstreamConnectionError):
        await retrying(inner, sleep, max_attempts=3).send(make_request(users_service))

    assert inner.calls == 3
    assert len(sleep.delays) == 2


async def test_retries_transient_status_codes(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(UNAVAILABLE, OK)

    response = await retrying(inner, sleep).send(make_request(users_service))

    assert response is OK
    assert inner.calls == 2


async def test_releases_the_streamed_responses_it_discards(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    discarded = FakeByteStream()
    inner = ScriptedUpstreamClient(UpstreamResponse(status_code=503, stream=discarded), OK)

    response = await retrying(inner, sleep).send(make_request(users_service))

    assert response is OK
    assert discarded.closed


async def test_returns_transient_status_when_attempts_are_exhausted(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(UNAVAILABLE)

    response = await retrying(inner, sleep, max_attempts=3).send(make_request(users_service))

    assert response is UNAVAILABLE
    assert inner.calls == 3


async def test_does_not_retry_non_transient_status_codes(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(UpstreamResponse(status_code=500))

    response = await retrying(inner, sleep).send(make_request(users_service))

    assert response.status_code == 500
    assert inner.calls == 1


@pytest.mark.parametrize("method", ["POST", "PATCH"])
async def test_never_retries_non_idempotent_methods(
    users_service: ServiceDefinition, sleep: RecordingSleep, method: str
) -> None:
    inner = ScriptedUpstreamClient(UpstreamConnectionError("users"))

    with pytest.raises(UpstreamConnectionError):
        await retrying(inner, sleep).send(make_request(users_service, method=method))

    assert inner.calls == 1


async def test_does_not_retry_unexpected_errors(
    users_service: ServiceDefinition, sleep: RecordingSleep
) -> None:
    inner = ScriptedUpstreamClient(RuntimeError("bug"))

    with pytest.raises(RuntimeError):
        await retrying(inner, sleep).send(make_request(users_service))

    assert inner.calls == 1


def test_backoff_grows_exponentially_up_to_the_cap() -> None:
    policy = RetryPolicy(base_delay_seconds=0.5, max_delay_seconds=3.0)

    assert [policy.delay_for(attempt) for attempt in range(1, 6)] == [0.5, 1.0, 2.0, 3.0, 3.0]


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"base_delay_seconds": -1}, {"max_delay_seconds": -1}],
)
def test_policy_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"must be at least 1|cannot be negative"):
        RetryPolicy(**kwargs)
