"""System endpoints: service identity and health probe.

These endpoints expose no sensitive data and need no authentication.
Keep them that way: do not add configuration values, versions of
dependencies, or environment details here.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__

router = APIRouter(tags=["system"])


class ServiceInfo(BaseModel):
    """Response body for GET /."""

    name: str
    version: str
    status: Literal["running"]


class HealthStatus(BaseModel):
    """Response body for GET /health."""

    status: Literal["ok"]


@router.get("/", response_model=ServiceInfo)
def read_root() -> ServiceInfo:
    """Return basic identity information about the service."""
    return ServiceInfo(name="AEGIS AI", version=__version__, status="running")


@router.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    """Liveness probe used by operators and, later, orchestrators."""
    return HealthStatus(status="ok")
