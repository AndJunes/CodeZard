import json

import pytest
from pydantic import ValidationError

from gateway.config.settings import ServiceSettings, Settings
from gateway.domain.models import ServiceDefinition


def test_loads_services_and_nested_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GATEWAY_SERVICES",
        json.dumps(
            [
                {"name": "users", "base_url": "http://users:8001/", "timeout_seconds": 3},
                {"name": "orders", "base_url": "http://orders:8002/v1", "health_path": "/ping"},
            ]
        ),
    )
    monkeypatch.setenv("GATEWAY_RETRY__MAX_ATTEMPTS", "5")
    monkeypatch.setenv("GATEWAY_CIRCUIT_BREAKER__FAILURE_THRESHOLD", "7")

    settings = Settings(_env_file=None)

    assert settings.service_definitions() == [
        ServiceDefinition(name="users", base_url="http://users:8001", timeout_seconds=3.0),
        ServiceDefinition(name="orders", base_url="http://orders:8002/v1", health_path="/ping"),
    ]
    assert settings.retry.max_attempts == 5
    assert settings.circuit_breaker.failure_threshold == 7


def test_defaults_are_production_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_SERVICES", raising=False)

    settings = Settings(_env_file=None)

    assert settings.services == []
    assert settings.retry.retry_on_status == {502, 503, 504}
    assert settings.circuit_breaker.failure_status_codes == {502, 503, 504}


@pytest.mark.parametrize("name", ["Users", "-users", "us ers", "users/admin", ""])
def test_rejects_invalid_service_names(name: str) -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(name=name, base_url="http://users:8001")


@pytest.mark.parametrize("base_url", ["users:8001", "ftp://users", "not a url"])
def test_rejects_invalid_base_urls(base_url: str) -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(name="users", base_url=base_url)


def test_service_headers_are_loaded_but_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "GATEWAY_SERVICES",
        json.dumps(
            [
                {
                    "name": "mirag",
                    "base_url": "http://mirag:8000",
                    "headers": {"X-Mirag-Token": "s3cret-value"},
                }
            ]
        ),
    )

    settings = Settings(_env_file=None)
    (definition,) = settings.service_definitions()

    assert definition.headers == (("X-Mirag-Token", "s3cret-value"),)
    assert "s3cret-value" not in repr(settings)
    assert "s3cret-value" not in repr(definition)


@pytest.mark.parametrize(
    "headers",
    [{"X Token": "t"}, {"": "t"}, {"X-Token:": "t"}, {"X-Token": ""}, {"X-Token": "a\nb"}],
)
def test_rejects_invalid_service_headers(headers: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(name="mirag", base_url="http://mirag:8000", headers=headers)


@pytest.mark.parametrize(
    "service",
    [
        # The header value itself is invalid.
        {"name": "mirag", "base_url": "http://mirag:8000", "headers": {"X-Token": "s3cret\r\n"}},
        # Another field is wrong, and the error would quote the whole entry.
        {"base_url": "http://mirag:8000", "headers": {"X-Token": "s3cret"}},
    ],
)
def test_configuration_errors_never_quote_header_values(
    monkeypatch: pytest.MonkeyPatch, service: dict[str, object]
) -> None:
    monkeypatch.setenv("GATEWAY_SERVICES", json.dumps([service]))

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert "services.0" in str(exc_info.value)
    assert "s3cret" not in str(exc_info.value)
