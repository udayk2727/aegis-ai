"""Read-only incident endpoints over the in-memory event store.

GET /api/v1/incidents and GET /api/v1/incidents/{incident_id} compute incidents
on request from the stored runs. Nothing is written, so these endpoints can
never change server state.

Every request must name at least one baseline run with ``baseline_run_id``. The
baseline is the normal behaviour the detector learns from. A baseline must be on
the trusted list loaded at startup (see ``trusted_baselines``). A caller cannot
add to that list, so an unapproved or faulty run is refused with
``baseline_not_approved`` (403) before the store is consulted. The pipeline
itself does not check labels. This layer enforces trust.

Errors carry a fixed code and message and no internal detail.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.domain.incident_records import Incident, IncidentId, IncidentSummary
from app.domain.telemetry import RunId
from app.routers.dependencies import get_event_store, get_trusted_baselines
from app.services.baseline_trust import TrustedBaselines
from app.services.event_store import EventStore
from app.services.incident_pipeline import ObservedRun, build_incidents

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_BASELINE_RUNS = 8

router = APIRouter(prefix="/api/v1", tags=["incidents"])


class IncidentPage(BaseModel):
    """Response body for GET /api/v1/incidents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[IncidentSummary]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]
    offset: Annotated[int, Field(ge=0)]


class IncidentErrorDetail(BaseModel):
    """Structured body for a 404 from the incident endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "baseline_not_approved",
        "baseline_run_not_found",
        "incident_not_found",
    ]
    message: str


BaselineRunIds = Annotated[
    list[RunId],
    Query(
        min_length=1,
        max_length=MAX_BASELINE_RUNS,
        description="Stored runs that define normal behaviour. Repeat the parameter.",
    ),
]


def _observed_runs(store: EventStore) -> list[ObservedRun]:
    """Snapshot the store as plain runs for the pipeline."""
    return [
        ObservedRun(
            run_id=run.run_id,
            tick_interval_seconds=run.spec.tick_interval_seconds,
            events=run.events,
        )
        for run in store.runs()
    ]


def _incidents_for(
    store: EventStore,
    trusted: TrustedBaselines,
    baseline_run_ids: list[str],
) -> list[Incident]:
    """Build incidents against approved baselines, refusing anything else.

    Approval is checked before the store, so a caller learns only that a baseline
    is not approved, not whether it is stored.
    """
    if not all(trusted.is_approved(run_id) for run_id in baseline_run_ids):
        refusal = IncidentErrorDetail(
            code="baseline_not_approved",
            message="a baseline run has not been approved for incident detection",
        )
        raise HTTPException(status_code=403, detail=refusal.model_dump())
    runs = _observed_runs(store)
    known = {run.run_id for run in runs}
    if any(run_id not in known for run_id in baseline_run_ids):
        detail = IncidentErrorDetail(
            code="baseline_run_not_found",
            message="a baseline run is not in the event store",
        )
        raise HTTPException(status_code=404, detail=detail.model_dump())
    return build_incidents(runs, baseline_run_ids, trusted=trusted)


@router.get(
    "/incidents",
    response_model=IncidentPage,
    responses={403: {"model": IncidentErrorDetail}, 404: {"model": IncidentErrorDetail}},
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
    incidents = _incidents_for(store, trusted, baseline_run_id)
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
    },
)
def get_incident(
    incident_id: Annotated[IncidentId, Path()],
    store: Annotated[EventStore, Depends(get_event_store)],
    trusted: Annotated[TrustedBaselines, Depends(get_trusted_baselines)],
    baseline_run_id: BaselineRunIds,
) -> Incident:
    """Return one incident with its evidence, timeline, and telemetry gaps."""
    for incident in _incidents_for(store, trusted, baseline_run_id):
        if incident.incident_id == incident_id:
            return incident
    detail = IncidentErrorDetail(
        code="incident_not_found",
        message="no incident with that ID for the given baselines",
    )
    raise HTTPException(status_code=404, detail=detail.model_dump())
