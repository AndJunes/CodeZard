import pytest

from gateway.domain.exceptions import InstanceNotFoundError
from gateway.domain.models import (
    HealthReport,
    HealthStatus,
    InstanceHealth,
    OutboundRequest,
    ServiceDefinition,
    ServiceHealth,
    ServiceInstance,
    UpstreamResponse,
)
from tests.fakes import FakeByteStream


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        ("http://users.internal", "/items", "http://users.internal/items"),
        ("http://users.internal/", "items", "http://users.internal/items"),
        ("http://users.internal/v1", "/items/1", "http://users.internal/v1/items/1"),
        ("http://users.internal", "", "http://users.internal/"),
        ("http://users.internal", "/a b/100%", "http://users.internal/a%20b/100%25"),
        ("http://users.internal", "/what?not-a-query", "http://users.internal/what%3Fnot-a-query"),
    ],
)
def test_url_joins_base_url_and_encoded_path(base_url: str, path: str, expected: str) -> None:
    request = OutboundRequest(
        service=ServiceDefinition(name="users", base_urls=(base_url,)), method="GET", path=path
    )

    assert request.url == expected


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", True),
        ("get", True),
        ("HEAD", True),
        ("OPTIONS", True),
        ("PUT", True),
        ("DELETE", True),
        ("POST", False),
        ("PATCH", False),
    ],
)
def test_is_idempotent(users_service: ServiceDefinition, method: str, expected: bool) -> None:
    request = OutboundRequest(service=users_service, method=method, path="/")

    assert request.is_idempotent is expected


UP = InstanceHealth(id="a", status=HealthStatus.UP)
DOWN = InstanceHealth(id="b", status=HealthStatus.DOWN)


@pytest.mark.parametrize(
    ("instances", "expected"),
    [
        ((UP,), HealthStatus.UP),
        ((UP, UP), HealthStatus.UP),
        ((UP, DOWN), HealthStatus.DEGRADED),
        ((DOWN, DOWN), HealthStatus.DOWN),
    ],
)
def test_service_status_sums_up_its_instances(
    instances: tuple[InstanceHealth, ...], expected: HealthStatus
) -> None:
    assert ServiceHealth(name="agent", instances=instances).status is expected


def test_health_report_is_healthy_only_when_every_service_is_up() -> None:
    up = ServiceHealth(name="users", instances=(UP,))
    degraded = ServiceHealth(name="orders", instances=(UP, DOWN))

    assert HealthReport(services=(up,)).is_healthy
    assert not HealthReport(services=(up, degraded)).is_healthy


def test_instance_ids_are_stable_short_and_opaque() -> None:
    instance = ServiceInstance("http://mirag-1:8000")

    assert instance.id == ServiceInstance("http://mirag-1:8000").id
    assert instance.id != ServiceInstance("http://mirag-2:8000").id
    assert len(instance.id) == 8
    assert "mirag" not in instance.id


def test_finds_an_instance_by_id() -> None:
    service = ServiceDefinition(name="agent", base_urls=("http://a:1", "http://b:2"))

    assert service.instance(service.instances[1].id).base_url == "http://b:2"
    with pytest.raises(InstanceNotFoundError, match="'agent' has no instance 'nope'"):
        service.instance("nope")


@pytest.mark.parametrize("base_urls", [(), ("http://a:1", "http://a:1")])
def test_rejects_services_without_distinct_servers(base_urls: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="'agent'"):
        ServiceDefinition(name="agent", base_urls=base_urls)


def test_url_uses_the_chosen_instance() -> None:
    service = ServiceDefinition(name="agent", base_urls=("http://a:1", "http://b:2"))
    request = OutboundRequest(
        service=service, method="GET", path="/x", instance=service.instances[1]
    )

    assert request.url == "http://b:2/x"
    assert request.target == f"agent@{service.instances[1].id}"


def test_url_needs_a_choice_only_when_there_are_several_instances(
    users_service: ServiceDefinition,
) -> None:
    several = ServiceDefinition(name="agent", base_urls=("http://a:1", "http://b:2"))

    assert OutboundRequest(service=users_service, method="GET", path="/x").url == (
        "http://users.internal/x"
    )
    with pytest.raises(RuntimeError, match="No instance of 'agent'"):
        _ = OutboundRequest(service=several, method="GET", path="/x").url


def test_empty_health_report_is_healthy() -> None:
    assert HealthReport(services=()).is_healthy


async def test_aclose_releases_a_streamed_body() -> None:
    stream = FakeByteStream(b"data: 1\n\n")

    await UpstreamResponse(status_code=200, stream=stream).aclose()

    assert stream.closed
