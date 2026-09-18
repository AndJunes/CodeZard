import pytest

from gateway.domain.models import ServiceDefinition


@pytest.fixture
def users_service() -> ServiceDefinition:
    return ServiceDefinition(name="users", base_url="http://users.internal", timeout_seconds=2.0)


@pytest.fixture
def orders_service() -> ServiceDefinition:
    return ServiceDefinition(name="orders", base_url="http://orders.internal/v1")
