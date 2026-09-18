from typing import Literal, Self

from pydantic import BaseModel

from gateway.domain.models import HealthReport, HealthStatus


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ServiceHealthResponse(BaseModel):
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


class ServicesHealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
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
