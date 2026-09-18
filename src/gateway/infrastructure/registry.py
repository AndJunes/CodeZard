from collections.abc import Iterable

from gateway.domain.exceptions import DuplicateServiceError, ServiceNotFoundError
from gateway.domain.models import ServiceDefinition
from gateway.domain.ports import ServiceRegistry


class InMemoryServiceRegistry(ServiceRegistry):
    """Static registry built from configuration at startup."""

    def __init__(self, services: Iterable[ServiceDefinition]) -> None:
        self._services: dict[str, ServiceDefinition] = {}
        for service in services:
            if service.name in self._services:
                raise DuplicateServiceError(service.name)
            self._services[service.name] = service

    def get(self, name: str) -> ServiceDefinition:
        try:
            return self._services[name]
        except KeyError:
            raise ServiceNotFoundError(name) from None

    def all(self) -> list[ServiceDefinition]:
        return list(self._services.values())
