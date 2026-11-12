"""Read-only incident endpoints over the in-memory event store.

GET /api/v1/incidents and GET /api/v1/incidents/{incident_id} compute incidents
on request from the stored runs. Nothing is written, so these endpoints can
never change server state.

Every request must name at least one baseline run with ``baseline_run_id``. The
baseline is the normal behaviour the detector learns from. The trust rules are
shared with the other incident-derived endpoints and live in ``incident_access``.

Errors carry a fixed code and message and no internal detail.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.domain.incident_records import Incident, IncidentId, IncidentSummary
from app.domain.telemetry import RunId
from app.routers.dependencies import get_event_store, get_trusted_baselines
from app.routers.incident_access import (
    BaselineRunIds,
    IncidentErrorDetail,
    incident_not_found,
    incidents_for,
    observed_runs,
)
from app.services.baseline_trust import TrustedBaselines
from app.services.event_store import EventStore

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

router = APIRouter(prefix="/api/v1", tags=["incidents"])


class IncidentPage(BaseModel):
    """Response body for GET /api/v1/incidents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[IncidentSummary]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]
    offset: Annotated[int, Field(ge=0)]


@router.get(
    "/incidents",
    response_model=IncidentPage,
    responses={
        403: {"model": IncidentErrorDetail},
        404: {"model": IncidentErrorDetail},
        500: {"model": IncidentErrorDetail},
    },
)
def list_incidents(
    store: Annotated[EventStore, Depends(get_event_store)],
    trusted: Annotated[TrustedBaselines, Depends(get_trusted_baselines)],
    baseline_run_id: BaselineRunIds,
    run_id: RunId | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentPage:
    """Return one page of incidents, ordered by first detection then ID.

    ``run_id`` keeps only incidents from that run. A baseline run is never
    scored, so naming one as ``run_id`` returns an empty page.
    """
    incidents = incidents_for(observed_runs(store), trusted, baseline_run_id)
    if run_id is not None:
        incidents = [item for item in incidents if item.source_run_id == run_id]
    page = incidents[offset : offset + limit]
    return IncidentPage(
        items=[IncidentSummary.from_incident(item) for item in page],
        total=len(incidents),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/incidents/{incident_id}",
    response_model=Incident,
    responses={
        403: {"model": IncidentErrorDetail},
        404: {"model": IncidentErrorDetail},
        500: {"model": IncidentErrorDetail},
    },
)
def get_incident(
    incident_id: Annotated[IncidentId, Path()],
    store: Annotated[EventStore, Depends(get_event_store)],
    trusted: Annotated[TrustedBaselines, Depends(get_trusted_baselines)],
    baseline_run_id: BaselineRunIds,
) -> Incident:
    """Return one incident with its evidence, timeline, and telemetry gaps."""
    for incident in incidents_for(observed_runs(store), trusted, baseline_run_id):
        if incident.incident_id == incident_id:
            return incident
    raise incident_not_found()
