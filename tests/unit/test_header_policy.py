import pytest

from gateway.application.header_policy import HeaderPolicy
from gateway.domain.models import Headers, InboundRequest


@pytest.fixture
def policy() -> HeaderPolicy:
    return HeaderPolicy()


def inbound(headers: Headers = (), **overrides: str | None) -> InboundRequest:
    return InboundRequest(method="GET", path="/", headers=headers, **overrides)  # type: ignore[arg-type]


def names(headers: Headers) -> list[str]:
    return [name.lower() for name, _ in headers]


def values_of(headers: Headers, name: str) -> list[str]:
    return [value for key, value in headers if key.lower() == name]


def test_keeps_end_to_end_headers(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound((("Accept", "application/json"), ("Authorization", "Bearer t")))
    )

    assert ("Accept", "application/json") in result
    assert ("Authorization", "Bearer t") in result


def test_drops_hop_by_hop_and_recomputed_headers(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound(
            (
                ("Connection", "keep-alive"),
                ("Keep-Alive", "timeout=5"),
                ("Transfer-Encoding", "chunked"),
                ("Upgrade", "websocket"),
                ("Host", "gateway.local"),
                ("Content-Length", "3"),
            )
        )
    )

    dropped = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length"}
    assert not dropped & set(names(result))


def test_drops_headers_listed_in_connection(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound(
            (
                ("Connection", "X-Internal-Token, close"),
                ("X-Internal-Token", "secret"),
                ("X-Other", "1"),
            )
        )
    )

    assert "x-internal-token" not in names(result)
    assert ("X-Other", "1") in result


def test_adds_forwarding_headers(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound(client_host="10.0.0.5", scheme="https", host="api.example.com", request_id="abc")
    )

    assert dict(result) == {
        "x-forwarded-proto": "https",
        "x-forwarded-for": "10.0.0.5",
        "x-forwarded-host": "api.example.com",
        "x-request-id": "abc",
    }


def test_appends_client_to_existing_forwarded_for_chain(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound((("X-Forwarded-For", "203.0.113.1"),), client_host="10.0.0.5")
    )

    assert values_of(result, "x-forwarded-for") == ["203.0.113.1, 10.0.0.5"]


def test_clients_cannot_spoof_forwarding_headers(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound(
            (
                ("X-Forwarded-Proto", "https"),
                ("X-Forwarded-Host", "evil.example.com"),
                ("X-Request-ID", "spoofed"),
            ),
            scheme="http",
            host="api.example.com",
            request_id="real",
        )
    )

    assert values_of(result, "x-forwarded-proto") == ["http"]
    assert values_of(result, "x-forwarded-host") == ["api.example.com"]
    assert values_of(result, "x-request-id") == ["real"]


def test_service_headers_replace_client_headers_with_the_same_name(policy: HeaderPolicy) -> None:
    result = policy.for_upstream(
        inbound((("x-mirag-token", "guessed"), ("Accept", "text/event-stream"))),
        service_headers=(("X-Mirag-Token", "secret"),),
    )

    assert values_of(result, "x-mirag-token") == ["secret"]
    assert ("Accept", "text/event-stream") in result


def test_for_client_strips_stale_and_hop_by_hop_headers_but_keeps_repeated_ones(
    policy: HeaderPolicy,
) -> None:
    result = policy.for_client(
        (
            ("content-encoding", "gzip"),
            ("content-length", "10"),
            ("transfer-encoding", "chunked"),
            ("connection", "close"),
            ("set-cookie", "a=1"),
            ("set-cookie", "b=2"),
            ("content-type", "application/json"),
        )
    )

    assert result == (
        ("set-cookie", "a=1"),
        ("set-cookie", "b=2"),
        ("content-type", "application/json"),
    )
