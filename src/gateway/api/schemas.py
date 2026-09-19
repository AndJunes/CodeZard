from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from gateway.domain.models import HealthReport, HealthStatus


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ServiceHealthResponse(BaseModel):
    name: str = Field(description="The service name, as registered in `GATEWAY_SERVICES`.")
    status: HealthStatus
    latency_ms: float | None = Field(
        default=None, description="Time the health check took. `null` when there was no answer."
    )
    detail: str | None = Field(default=None, description="Why the service is `down`.")


class ServicesHealthResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "degraded",
                    "services": [
                        {"name": "httpbin", "status": "up", "latency_ms": 3.41, "detail": None},
                        {
                            "name": "mirag",
                            "status": "down",
                            "latency_ms": None,
                            "detail": "Service 'mirag' is unreachable",
                        },
                    ],
                }
            ]
        }
    )

    status: Literal["ok", "degraded"] = Field(
        description="`ok` when every service is `up`, `degraded` otherwise."
    )
    services: list[ServiceHealthResponse]

    @classmethod
    def from_report(cls, report: HealthReport) -> Self:
        return cls(
            status="ok" if report.is_healthy else "degraded",
            services=[
                ServiceHealthResponse(
                    name=service.name,
                    status=service.status,
                    latency_ms=service.latency_ms,
                    detail=service.detail,
                )
                for service in report.services
            ],
        )


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable, machine-readable identifier of the error.")
    message: str = Field(description="Human-readable explanation. Do not parse it.")
    request_id: str | None = Field(
        default=None, description="The `X-Request-ID` of the request, to find it in the logs."
    )


class ErrorResponse(BaseModel):
    """The body of every error produced by the gateway itself."""

    error: ErrorDetail
