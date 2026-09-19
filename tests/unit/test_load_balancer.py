import dataclasses

import pytest

from gateway.domain.exceptions import (
    CircuitOpenError,
    UpstreamConnectionError,
    UpstreamTimeoutError,
)
from gateway.domain.models import (
    OutboundRequest,
    ServiceDefinition,
    ServiceInstance,
    UpstreamResponse,
)
from gateway.domain.ports import UpstreamClient
from gateway.infrastructure.resilience.load_balancer import LoadBalancingUpstreamClient
from tests.fakes import Outcome, ScriptedUpstreamClient, make_request

AGENT = ServiceDefinition(name="agent", base_urls=("http://a", "http://b", "http://c"))
A, B, C = AGENT.instances
OK = UpstreamResponse(status_code=200)


class PerInstance(UpstreamClient):
    """Plays every instance: each one answers with its own script (``OK`` by default)."""

    def __init__(self, **scripts: Outcome) -> None:
        self._clients = {
            instance: ScriptedUpstreamClient(scripts.get(name, OK))
            for name, instance in zip("abc", AGENT.instances, strict=True)
        }
        self.sent_to: list[ServiceInstance] = []

    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        assert request.instance is not None, "the balancer must choose an instance"
        self.sent_to.append(request.instance)
        return await self._clients[request.instance].send(request)


def pinned(instance: ServiceInstance, method: str = "GET") -> OutboundRequest:
    return dataclasses.replace(make_request(AGENT, method), instance=instance)


async def test_spreads_requests_round_robin() -> None:
    inner = PerInstance()
    balancer = LoadBalancingUpstreamClient(inner)

    for _ in range(4):
        await balancer.send(make_request(AGENT))

    assert inner.sent_to == [A, B, C, A]


async def test_tags_the_response_with_the_instance_that_answered() -> None:
    balancer = LoadBalancingUpstreamClient(PerInstance())

    first = await balancer.send(make_request(AGENT))
    second = await balancer.send(make_request(AGENT))

    assert (first.instance_id, second.instance_id) == (A.id, B.id)


async def test_each_service_takes_its_own_turns(users_service: ServiceDefinition) -> None:
    inner = ScriptedUpstreamClient(OK)
    balancer = LoadBalancingUpstreamClient(inner)

    for service in (AGENT, users_service, AGENT):
        await balancer.send(make_request(service))

    assert [request.instance for request in inner.requests] == [
        A,
        users_service.instances[0],
        B,
    ]


async def test_skips_an_instance_whose_circuit_is_open() -> None:
    inner = PerInstance(a=CircuitOpenError("agent"))

    response = await LoadBalancingUpstreamClient(inner).send(make_request(AGENT))

    assert response.instance_id == B.id


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"])
async def test_fails_over_idempotent_requests_on_any_upstream_error(method: str) -> None:
    inner = PerInstance(a=UpstreamTimeoutError("agent"))

    response = await LoadBalancingUpstreamClient(inner).send(make_request(AGENT, method))

    assert response.instance_id == B.id
    assert inner.sent_to == [A, B]


@pytest.mark.parametrize(
    "never_received",
    [
        UpstreamConnectionError("agent", request_sent=False),
        UpstreamTimeoutError("agent", request_sent=False),
    ],
)
async def test_fails_over_a_post_the_failed_instance_never_received(
    never_received: Exception,
) -> None:
    inner = PerInstance(a=never_received)

    response = await LoadBalancingUpstreamClient(inner).send(make_request(AGENT, "POST"))

    assert response.instance_id == B.id


@pytest.mark.parametrize(
    "maybe_received", [UpstreamConnectionError("agent"), UpstreamTimeoutError("agent")]
)
async def test_never_repeats_a_post_the_failed_instance_may_have_received(
    maybe_received: Exception,
) -> None:
    inner = PerInstance(a=maybe_received)

    with pytest.raises(type(maybe_received)):
        await LoadBalancingUpstreamClient(inner).send(make_request(AGENT, "POST"))

    assert inner.sent_to == [A]


async def test_an_error_status_is_an_answer_not_a_failover() -> None:
    inner = PerInstance(a=UpstreamResponse(status_code=503))

    response = await LoadBalancingUpstreamClient(inner).send(make_request(AGENT))

    assert (response.status_code, response.instance_id) == (503, A.id)
    assert inner.sent_to == [A]


async def test_raises_the_failure_of_an_instance_that_was_tried() -> None:
    inner = PerInstance(
        a=CircuitOpenError("agent"),
        b=UpstreamConnectionError("agent", request_sent=False),
        c=CircuitOpenError("agent"),
    )

    with pytest.raises(UpstreamConnectionError):
        await LoadBalancingUpstreamClient(inner).send(make_request(AGENT, "POST"))

    assert inner.sent_to == [A, B, C]


async def test_answers_circuit_open_when_every_circuit_is_open() -> None:
    open_circuit = CircuitOpenError("agent")
    inner = PerInstance(a=open_circuit, b=open_circuit, c=open_circuit)

    with pytest.raises(CircuitOpenError):
        await LoadBalancingUpstreamClient(inner).send(make_request(AGENT))


async def test_a_pinned_request_goes_to_its_instance_only() -> None:
    inner = PerInstance()
    balancer = LoadBalancingUpstreamClient(inner)

    response = await balancer.send(pinned(C))

    assert inner.sent_to == [C]
    assert response.instance_id == C.id


async def test_a_pinned_request_never_fails_over() -> None:
    # It asks for something only that instance has: another one would answer wrongly.
    inner = PerInstance(b=UpstreamConnectionError("agent", request_sent=False))

    with pytest.raises(UpstreamConnectionError):
        await LoadBalancingUpstreamClient(inner).send(pinned(B))

    assert inner.sent_to == [B]
