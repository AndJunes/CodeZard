"""Domain errors. They describe *what* failed; the API layer decides the HTTP status."""


class GatewayError(Exception):
    """Base class for every error raised by the gateway."""


class ServiceNotFoundError(GatewayError):
    def __init__(self, service_name: str) -> None:
        super().__init__(f"Service '{service_name}' is not registered")
        self.service_name = service_name


class DuplicateServiceError(GatewayError):
    def __init__(self, service_name: str) -> None:
        super().__init__(f"Service '{service_name}' is registered more than once")
        self.service_name = service_name


class UpstreamError(GatewayError):
    """The downstream service could not produce a response."""

    def __init__(self, service_name: str, message: str) -> None:
        super().__init__(message)
        self.service_name = service_name


class UpstreamTimeoutError(UpstreamError):
    def __init__(self, service_name: str) -> None:
        super().__init__(service_name, f"Service '{service_name}' did not respond in time")


class UpstreamConnectionError(UpstreamError):
    def __init__(self, service_name: str) -> None:
        super().__init__(service_name, f"Service '{service_name}' is unreachable")


class CircuitOpenError(GatewayError):
    """Calls are being rejected to give a failing service time to recover."""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"Service '{service_name}' is temporarily unavailable")
        self.service_name = service_name
