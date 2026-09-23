import pytest

from gateway.domain.models import ServiceDefinition


@pytest.fixture
def users_service() -> ServiceDefinition:
    return ServiceDefinition(
        name="users", base_urls=("http://users.internal",), timeout_seconds=2.0
    )


@pytest.fixture
def orders_service() -> ServiceDefinition:
    return ServiceDefinition(name="orders", base_urls=("http://orders.internal/v1",))
