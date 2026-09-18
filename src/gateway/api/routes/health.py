from fastapi import APIRouter

from gateway.api.dependencies import HealthServiceDep
from gateway.api.schemas import LivenessResponse, ServicesHealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="Gateway liveness")
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get("/services", summary="Health of every downstream service")
async def services_health(health_service: HealthServiceDep) -> ServicesHealthResponse:
    # Always 200: a downstream outage must not mark the gateway itself as unhealthy.
    report = await health_service.check_all()
    return ServicesHealthResponse.from_report(report)
