"""Read-only investigation endpoint for one incident.

GET /api/v1/incidents/{incident_id}/investigation builds the investigation report
for an incident on request. It writes nothing and calls no external service, so
it cannot change server state or send telemetry anywhere.

The request names the same approved baselines as the incident endpoints, and
the same trust rules apply. The incident and the source run's verdicts come from
one snapshot of the store, so they always refer to the same data.

Before a report is returned, every cited reference is checked against the stored
events of its run. A report that fails the check is an internal fault, so it is
answered with the generic 500 and logged, not served and not described.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.domain.incident_records import Incident, IncidentId
from app.domain.investigation_records import Investigation
from app.routers.dependencies import (
    get_event_store,
    get_investigation_cap,
    get_trusted_baselines,
)
from app.routers.incident_access import (
    BaselineRunIds,
    IncidentErrorDetail,
    analysis_failed,
    incident_not_found,
    incidents_for,
    observed_runs,
)
from app.services.baseline_trust import TrustedBaselines
from app.services.event_store import EventStore
from app.services.incident_pipeline import ObservedRun, score_run
from app.services.investigation import investigate
from app.services.investigation_provenance import provenance_violations

router = APIRouter(prefix="/api/v1", tags=["investigations"])


@router.get(
    "/incidents/{incident_id}/investigation",
    response_model=Investigation,
    responses={
        403: {"model": IncidentErrorDetail},
        404: {"model": IncidentErrorDetail},
        500: {"model": IncidentErrorDetail},
    },
)
def get_investigation(
    incident_id: Annotated[IncidentId, Path()],
    store: Annotated[EventStore, Depends(get_event_store)],
    trusted: Annotated[TrustedBaselines, Depends(get_trusted_baselines)],
    cap: Annotated[int, Depends(get_investigation_cap)],
    baseline_run_id: BaselineRunIds,
) -> Investigation:
    """Return the evidence-based investigation report for one incident."""
    runs = observed_runs(store)
    for incident in incidents_for(runs, trusted, baseline_run_id):
        if incident.incident_id == incident_id:
            return _checked_report(
                runs,
                incident,
                trusted=trusted,
                baseline_run_ids=baseline_run_id,
                max_cited_event_ids=cap,
            )
    raise incident_not_found()


def _checked_report(
    runs: list[ObservedRun],
    incident: Incident,
    *,
    trusted: TrustedBaselines,
    baseline_run_ids: list[str],
    max_cited_event_ids: int,
) -> Investigation:
    """Build the report from the source run's verdicts and check its provenance.

    An analysis failure, or a provenance violation, is logged and turned into the
    generic 500. Nothing from the report is included in the error.
    """
    try:
        verdicts = score_run(
            runs,
            baseline_run_ids,
            incident.source_run_id,
            trusted=trusted,
        )
        report = investigate(
            incident,
            verdicts,
            max_cited_event_ids=max_cited_event_ids,
        )
    except (ValueError, RuntimeError) as error:
        raise analysis_failed(str(error)) from error

    source = next(run for run in runs if run.run_id == incident.source_run_id)
    violations = provenance_violations(
        report,
        {event.event_id: event for event in source.events},
    )
    if violations:
        raise analysis_failed(
            f"{report.investigation_id} failed provenance with {len(violations)} "
            f"violation(s), first: {violations[0]}"
        )
    return report
