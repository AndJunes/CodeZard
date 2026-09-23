"""Rules deciding which headers cross the gateway in each direction."""

from gateway.domain.models import Headers, InboundRequest

# RFC 9110 §7.6.1: meaningful only for a single connection, never forwarded.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Recomputed by the HTTP client for the outbound connection.
_RECOMPUTED_REQUEST_HEADERS = frozenset({"host", "content-length"})

# Rewritten by the gateway so clients cannot spoof them.
_FORWARDING_HEADERS = frozenset(
    {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-request-id"}
)

# The HTTP client already decoded the body, so the original encoding and length no longer apply.
_STALE_RESPONSE_HEADERS = frozenset({"content-encoding", "content-length"})


class HeaderPolicy:
    """Filters headers and adds the standard proxy forwarding headers."""

    def for_upstream(self, inbound: InboundRequest, service_headers: Headers = ()) -> Headers:
        """``service_headers`` (configured per service) replace any client header of the same
        name, so a client can neither read nor override a credential the gateway injects."""
        dropped = (
            HOP_BY_HOP_HEADERS
            | _RECOMPUTED_REQUEST_HEADERS
            | _FORWARDING_HEADERS
            | _connection_tokens(inbound.headers)
            | {name.lower() for name, _ in service_headers}
        )
        kept = [(name, value) for name, value in inbound.headers if name.lower() not in dropped]
        return (*kept, *self._forwarding_headers(inbound), *service_headers)

    def for_client(self, headers: Headers) -> Headers:
        dropped = HOP_BY_HOP_HEADERS | _STALE_RESPONSE_HEADERS | _connection_tokens(headers)
        return tuple((name, value) for name, value in headers if name.lower() not in dropped)

    @staticmethod
    def _forwarding_headers(inbound: InboundRequest) -> Headers:
        chain = [value for name, value in inbound.headers if name.lower() == "x-forwarded-for"]
        if inbound.client_host:
            chain.append(inbound.client_host)

        headers: list[tuple[str, str]] = [("x-forwarded-proto", inbound.scheme)]
        if chain:
            headers.append(("x-forwarded-for", ", ".join(chain)))
        if inbound.host:
            headers.append(("x-forwarded-host", inbound.host))
        if inbound.request_id:
            headers.append(("x-request-id", inbound.request_id))
        return tuple(headers)


def _connection_tokens(headers: Headers) -> frozenset[str]:
    """Headers named in ``Connection`` are hop-by-hop too (RFC 9110 §7.6.1)."""
    return frozenset(
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    )
