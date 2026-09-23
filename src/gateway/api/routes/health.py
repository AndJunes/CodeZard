from fastapi import APIRouter

from gateway.api.dependencies import HealthServiceDep
from gateway.api.schemas import LivenessResponse, ServicesHealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "",
    operation_id="health_liveness",
    summary="Gateway liveness",
    description="`200` while the gateway process is up. It calls no service, so it is cheap "
    "enough for a container health check (the Dockerfile's `HEALTHCHECK` uses it).",
)
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/services",
    operation_id="health_services",
    summary="Health of every downstream service",
    description="Calls the `health_path` of every registered service in parallel, with the "
    "service's configured headers but **without** retries or circuit breaker, so it reports "
    "the real state. A service is `up` when it answers `2xx`.\n\n"
    "Always `200`: a service being down makes the report `degraded`, never the gateway "
    "unhealthy.",
)
async def services_health(health_service: HealthServiceDep) -> ServicesHealthResponse:
    # Always 200: a downstream outage must not mark the gateway itself as unhealthy.
    report = await health_service.check_all()
    return ServicesHealthResponse.from_report(report)
