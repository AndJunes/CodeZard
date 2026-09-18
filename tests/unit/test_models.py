import pytest

from gateway.domain.models import (
    HealthReport,
    HealthStatus,
    OutboundRequest,
    ServiceDefinition,
    ServiceHealth,
)


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
        service=ServiceDefinition(name="users", base_url=base_url), method="GET", path=path
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


def test_health_report_is_healthy_only_when_every_service_is_up() -> None:
    up = ServiceHealth(name="users", status=HealthStatus.UP)
    down = ServiceHealth(name="orders", status=HealthStatus.DOWN)

    assert HealthReport(services=(up,)).is_healthy
    assert not HealthReport(services=(up, down)).is_healthy


def test_empty_health_report_is_healthy() -> None:
    assert HealthReport(services=()).is_healthy
