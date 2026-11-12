"""Access rules shared by every endpoint that derives data from incidents.

Each such endpoint reads one snapshot of the store and enforces the same
baseline trust before any analysis runs. A baseline must be on the trusted list
loaded at startup, and a caller cannot add to that list. An unapproved run is
refused with ``baseline_not_approved`` before the store is consulted, so a caller
cannot learn whether an unapproved run is stored.

Errors carry a fixed code and message and no internal detail. A failed analysis
is an internal fault, so it returns a 500 with the fixed ``analysis_failed`` code.
The cause is logged on the server and never sent to the caller.
"""

import logging
from collections.abc import Sequence
from typing import Annotated, Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.domain.incident_records import Incident
from app.domain.telemetry import RunId
from app.services.baseline_trust import TrustedBaselines
from app.services.event_store import EventStore
from app.services.incident_pipeline import ObservedRun, build_incidents

MAX_BASELINE_RUNS = 8

logger = logging.getLogger(__name__)


class IncidentErrorDetail(BaseModel):
    """Structured body for a 403, 404, or 500 from the incident-derived endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "baseline_not_approved",
        "baseline_run_not_found",
        "incident_not_found",
        "analysis_failed",
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


def observed_runs(store: EventStore) -> list[ObservedRun]:
    """Snapshot the store as plain runs. Take one snapshot per request."""
    return [
        ObservedRun(
            run_id=run.run_id,
            tick_interval_seconds=run.spec.tick_interval_seconds,
            events=run.events,
        )
        for run in store.runs()
    ]


def incidents_for(
    runs: Sequence[ObservedRun],
    trusted: TrustedBaselines,
    baseline_run_ids: list[str],
) -> list[Incident]:
    """Build incidents against approved baselines, refusing anything else.

    Raises HTTPException with a structured body when a baseline is not approved
    (403) or not stored (404).
    """
    if not all(trusted.is_approved(run_id) for run_id in baseline_run_ids):
        refusal = IncidentErrorDetail(
            code="baseline_not_approved",
            message="a baseline run has not been approved for incident detection",
        )
        raise HTTPException(status_code=403, detail=refusal.model_dump())
    known = {run.run_id for run in runs}
    if any(run_id not in known for run_id in baseline_run_ids):
        detail = IncidentErrorDetail(
            code="baseline_run_not_found",
            message="a baseline run is not in the event store",
        )
        raise HTTPException(status_code=404, detail=detail.model_dump())
    try:
        return build_incidents(runs, baseline_run_ids, trusted=trusted)
    except (ValueError, RuntimeError) as error:
        raise analysis_failed(str(error)) from error


def incident_not_found() -> HTTPException:
    """Return the 404 raised when no incident matches for the given baselines."""
    detail = IncidentErrorDetail(
        code="incident_not_found",
        message="no incident with that ID for the given baselines",
    )
    return HTTPException(status_code=404, detail=detail.model_dump())


def analysis_failed(reason: str) -> HTTPException:
    """Log an internal analysis failure and return a generic 500.

    The reason is written to the server log only. It names identifiers, never
    payloads. The response does not say what failed, because the failure is ours
    and not a mistake in the request.
    """
    logger.error("incident analysis failed: %s", reason)
    detail = IncidentErrorDetail(
        code="analysis_failed",
        message="the incident analysis could not be completed",
    )
    return HTTPException(status_code=500, detail=detail.model_dump())
