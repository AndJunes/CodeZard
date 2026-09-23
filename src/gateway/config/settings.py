"""Settings loaded from environment variables (prefix ``GATEWAY_``) or a ``.env`` file.

Nested values use ``__`` as delimiter, e.g. ``GATEWAY_RETRY__MAX_ATTEMPTS=5``.
Lists are given as JSON, e.g.
``GATEWAY_SERVICES='[{"name": "users", "base_url": "http://users:8001"}]'``.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.domain.models import ServiceDefinition

_DEFAULT_TRANSIENT_STATUS = frozenset({502, 503, 504})


class ServiceSettings(BaseModel):
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
    base_url: HttpUrl
    timeout_seconds: PositiveFloat = 5.0
    health_path: str = "/health"
    read_timeout_seconds: PositiveFloat | None = None
    """Seconds allowed between two chunks of a streamed response.

    Defaults to ``timeout_seconds``. A service that streams events wants this generous and
    ``timeout_seconds`` short: the first bounds silence between events, the second bounds
    connecting.
    """

    def to_definition(self) -> ServiceDefinition:
        return ServiceDefinition(
            name=self.name,
            base_url=str(self.base_url).rstrip("/"),
            timeout_seconds=self.timeout_seconds,
            health_path=self.health_path,
            read_timeout_seconds=self.read_timeout_seconds,
        )


class OrchestrationSettings(BaseModel):
    """Which services the run orchestrator talks to, and with what.

    The tokens live HERE and not in the browser's server, which is the whole point of moving
    the orchestration: today the page's Astro server holds both agent tokens and hands them
    to the gateway on every call, so the gateway is a pipe and the caller is trusted. Once
    the gateway decides, the gateway is the one that has to authenticate.

    Empty tokens are allowed because a local stack runs its agents open, and they say so at
    startup. There is no default token on purpose: a factory secret is known to everybody.
    """

    enabled: bool = False
    """Off unless asked for. A gateway with no agents behind it should not advertise routes
    that answer 502 to everything."""
    pm_service: str = "pm"
    backend_service: str = "backend"
    pm_token: str = ""
    backend_token: str = ""
    locale: str = "es"

    # There is deliberately no timeout here. A PM call was measured at 246 seconds on a free
    # model that had to be asked twice, and it is already bounded by the service's own
    # `read_timeout_seconds` — which the orchestrator reaches through the same ProxyService as
    # everything else. A second knob for the same thing is a knob that will disagree with the
    # first one.


class RetrySettings(BaseModel):
    max_attempts: PositiveInt = 3
    base_delay_seconds: NonNegativeFloat = 0.1
    max_delay_seconds: NonNegativeFloat = 2.0
    retry_on_status: frozenset[int] = _DEFAULT_TRANSIENT_STATUS


class CircuitBreakerSettings(BaseModel):
    failure_threshold: PositiveInt = 5
    recovery_timeout_seconds: PositiveFloat = 30.0
    failure_status_codes: frozenset[int] = _DEFAULT_TRANSIENT_STATUS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "API Gateway"
    host: str = "0.0.0.0"
    port: PositiveInt = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    max_connections: PositiveInt = 100
    services: list[ServiceSettings] = Field(default_factory=list)
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)

    def service_definitions(self) -> list[ServiceDefinition]:
        return [service.to_definition() for service in self.services]


@lru_cache
def get_settings() -> Settings:
    return Settings()
