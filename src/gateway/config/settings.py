"""Settings loaded from environment variables (prefix ``GATEWAY_``) or a ``.env`` file.

Nested values use ``__`` as delimiter, e.g. ``GATEWAY_RETRY__MAX_ATTEMPTS=5``.
Lists are given as JSON, e.g.
``GATEWAY_SERVICES='[{"name": "users", "base_url": "http://users:8001"}]'``.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    StringConstraints,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.domain.models import ServiceDefinition

_DEFAULT_TRANSIENT_STATUS = frozenset({502, 503, 504})

# RFC 9110 §5.6.2 token.
HeaderName = Annotated[str, StringConstraints(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")]


class ServiceSettings(BaseModel):
    # Validation errors quote the offending input, and here that input can hold a credential
    # (`headers`) that would end up in the startup logs. The location and reason still show.
    model_config = ConfigDict(hide_input_in_errors=True)

    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
    base_url: HttpUrl
    timeout_seconds: PositiveFloat = 5.0
    health_path: str = "/health"
    headers: dict[HeaderName, SecretStr] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def _header_values_are_single_line(cls, headers: dict[str, SecretStr]) -> dict[str, SecretStr]:
        for name, value in headers.items():
            secret = value.get_secret_value()
            # A line break would let the value inject extra headers.
            if not secret or any(char in secret for char in "\r\n\0"):
                raise ValueError(f"header '{name}' must be a non-empty single line")
        return headers

    def to_definition(self) -> ServiceDefinition:
        return ServiceDefinition(
            name=self.name,
            base_url=str(self.base_url).rstrip("/"),
            timeout_seconds=self.timeout_seconds,
            health_path=self.health_path,
            headers=tuple((name, value.get_secret_value()) for name, value in self.headers.items()),
        )


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
        # Errors are raised by this model even when they happen inside a service entry, so
        # hiding inputs there alone would still quote the entry, headers included.
        hide_input_in_errors=True,
    )

    app_name: str = "API Gateway"
    host: str = "0.0.0.0"
    port: PositiveInt = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    max_connections: PositiveInt = 100
    services: list[ServiceSettings] = Field(default_factory=list)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)

    def service_definitions(self) -> list[ServiceDefinition]:
        return [service.to_definition() for service in self.services]


@lru_cache
def get_settings() -> Settings:
    return Settings()
