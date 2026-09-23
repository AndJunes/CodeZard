"""Settings loaded from environment variables (prefix ``GATEWAY_``) or a ``.env`` file.

Nested values use ``__`` as delimiter, e.g. ``GATEWAY_RETRY__MAX_ATTEMPTS=5``.
Lists are given as JSON, e.g.
``GATEWAY_SERVICES='[{"name": "users", "base_url": "http://users:8001"}]'``, or with several
instances of a service, ``"base_urls": ["http://users-1:8001", "http://users-2:8001"]``.
"""

from functools import lru_cache
from typing import Annotated, Literal, Self

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
    model_validator,
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
    # One of the two: a single server, or several interchangeable instances of the service.
    base_url: HttpUrl | None = None
    base_urls: list[HttpUrl] = Field(default_factory=list)
    timeout_seconds: PositiveFloat = 5.0
    health_path: str = "/health"
    read_timeout_seconds: PositiveFloat | None = None
    """Seconds allowed between two chunks of a streamed response.

    Defaults to ``timeout_seconds``. A service that streams events wants this generous and
    ``timeout_seconds`` short: the first bounds silence between events, the second bounds
    connecting.
    """
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

    @model_validator(mode="after")
    def _servers_are_given_once(self) -> Self:
        if (self.base_url is None) == (not self.base_urls):
            raise ValueError("set exactly one of 'base_url' and 'base_urls'")
        urls = self._normalized_urls()
        if len(set(urls)) != len(urls):
            raise ValueError("'base_urls' lists the same server twice")
        return self

    def _normalized_urls(self) -> tuple[str, ...]:
        urls = self.base_urls if self.base_url is None else [self.base_url]
        return tuple(str(url).rstrip("/") for url in urls)

    def to_definition(self) -> ServiceDefinition:
        return ServiceDefinition(
            name=self.name,
            base_urls=self._normalized_urls(),
            timeout_seconds=self.timeout_seconds,
            health_path=self.health_path,
            read_timeout_seconds=self.read_timeout_seconds,
            headers=tuple((name, value.get_secret_value()) for name, value in self.headers.items()),
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
    console: bool = False
    """Let the browser run commands against a run's generated project (`POST /runs/{id}/console`).

    Off by default and separate from `enabled`: orchestration talks to agents the operator chose,
    this puts a terminal on the operator's machine. It also needs the agent's own switch
    (`MIRAG_CONSOLE`) — the gateway only forwards, it does not execute anything itself."""

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
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)

    def service_definitions(self) -> list[ServiceDefinition]:
        return [service.to_definition() for service in self.services]


@lru_cache
def get_settings() -> Settings:
    return Settings()
