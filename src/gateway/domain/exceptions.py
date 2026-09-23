"""Domain errors. They describe *what* failed; the API layer decides the HTTP status."""


class GatewayError(Exception):
    """Base class for every error raised by the gateway."""


class ServiceNotFoundError(GatewayError):
    def __init__(self, service_name: str) -> None:
        super().__init__(f"Service '{service_name}' is not registered")
        self.service_name = service_name


class InstanceNotFoundError(GatewayError):
    def __init__(self, service_name: str, instance_id: str) -> None:
        super().__init__(f"Service '{service_name}' has no instance '{instance_id}'")
        self.service_name = service_name
        self.instance_id = instance_id


class DuplicateServiceError(GatewayError):
    def __init__(self, service_name: str) -> None:
        super().__init__(f"Service '{service_name}' is registered more than once")
        self.service_name = service_name


class UpstreamError(GatewayError):
    """The downstream service could not produce a response."""

    def __init__(self, service_name: str, message: str, *, request_sent: bool = True) -> None:
        super().__init__(message)
        self.service_name = service_name
        self.request_sent = request_sent
        """``False`` when the connection was never established: the service cannot have acted
        on the request, so sending it elsewhere is safe even for a ``POST``."""


class UpstreamTimeoutError(UpstreamError):
    def __init__(self, service_name: str, *, request_sent: bool = True) -> None:
        super().__init__(
            service_name,
            f"Service '{service_name}' did not respond in time",
            request_sent=request_sent,
        )


class UpstreamConnectionError(UpstreamError):
    def __init__(self, service_name: str, *, request_sent: bool = True) -> None:
        super().__init__(
            service_name, f"Service '{service_name}' is unreachable", request_sent=request_sent
        )


class CircuitOpenError(GatewayError):
    """Calls are being rejected to give a failing service time to recover."""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"Service '{service_name}' is temporarily unavailable")
        self.service_name = service_name


class RunNotFoundError(GatewayError):
    """No such run, or it expired.

    The two are deliberately the same answer. Telling them apart would say whether an id ever
    existed, and a run id is the only thing protecting a run.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__("That run does not exist, or it expired")
        self.run_id = run_id
