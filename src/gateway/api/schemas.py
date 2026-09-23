from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from gateway.domain.models import HealthReport, HealthStatus


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class InstanceHealthResponse(BaseModel):
    id: str = Field(
        description="The instance id. `/api/{service}@{id}/...` sends a request to this server."
    )
    status: Literal[HealthStatus.UP, HealthStatus.DOWN]
    latency_ms: float | None = Field(
        default=None, description="Time the health check took. `null` when there was no answer."
    )
    detail: str | None = Field(default=None, description="Why the instance is `down`.")


class ServiceHealthResponse(BaseModel):
    name: str = Field(description="The service name, as registered in `GATEWAY_SERVICES`.")
    status: HealthStatus = Field(
        description="`up` when every instance is up, `down` when none is, `degraded` otherwise."
    )
    instances: list[InstanceHealthResponse]


class ServicesHealthResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "degraded",
                    "services": [
                        {
                            "name": "httpbin",
                            "status": "up",
                            "instances": [
                                {
                                    "id": "0e5a7c21",
                                    "status": "up",
                                    "latency_ms": 3.41,
                                    "detail": None,
                                }
                            ],
                        },
                        {
                            "name": "mirag",
                            "status": "degraded",
                            "instances": [
                                {
                                    "id": "3fa1c2d0",
                                    "status": "up",
                                    "latency_ms": 8.02,
                                    "detail": None,
                                },
                                {
                                    "id": "9b2e4d11",
                                    "status": "down",
                                    "latency_ms": None,
                                    "detail": "Service 'mirag' is unreachable",
                                },
                            ],
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
                    instances=[
                        InstanceHealthResponse(
                            id=instance.id,
                            status=instance.status,
                            latency_ms=instance.latency_ms,
                            detail=instance.detail,
                        )
                        for instance in service.instances
                    ],
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
