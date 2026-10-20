"""Liveness endpoint."""

from fastapi import APIRouter

from app.core.time import isoformat_utc, utcnow
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(status="HEALTHY", current_time=isoformat_utc(utcnow()))
