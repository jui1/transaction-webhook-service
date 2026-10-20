"""Health check response."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["HEALTHY"]
    current_time: str
