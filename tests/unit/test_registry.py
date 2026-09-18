import pytest

from gateway.domain.exceptions import DuplicateServiceError, ServiceNotFoundError
from gateway.domain.models import ServiceDefinition
from gateway.infrastructure.registry import InMemoryServiceRegistry


def test_get_returns_registered_service(
    users_service: ServiceDefinition, orders_service: ServiceDefinition
) -> None:
    registry = InMemoryServiceRegistry([users_service, orders_service])

    assert registry.get("users") is users_service
    assert registry.get("orders") is orders_service


def test_get_unknown_service_raises() -> None:
    registry = InMemoryServiceRegistry([])

    with pytest.raises(ServiceNotFoundError, match="'billing'") as exc_info:
        registry.get("billing")

    assert exc_info.value.service_name == "billing"


def test_all_returns_services_in_registration_order(
    users_service: ServiceDefinition, orders_service: ServiceDefinition
) -> None:
    registry = InMemoryServiceRegistry([users_service, orders_service])

    assert registry.all() == [users_service, orders_service]


def test_duplicate_service_names_are_rejected(users_service: ServiceDefinition) -> None:
    with pytest.raises(DuplicateServiceError, match="'users'"):
        InMemoryServiceRegistry([users_service, users_service])
