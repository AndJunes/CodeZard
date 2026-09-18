"""FastAPI dependencies resolving use cases from the container built at startup."""

from typing import Annotated

from fastapi import Depends, Request

from gateway.application.health_service import HealthService
from gateway.application.proxy_service import ProxyService
from gateway.bootstrap import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_proxy_service(container: Annotated[Container, Depends(get_container)]) -> ProxyService:
    return container.proxy_service


def get_health_service(container: Annotated[Container, Depends(get_container)]) -> HealthService:
    return container.health_service


ProxyServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
