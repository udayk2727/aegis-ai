"""Demo-only, read-only listing of approved synthetic baseline runs.

GET /api/v1/demo/baselines lists the approved baseline runs that are also stored
in the event store. The incident and investigation endpoints require each
``baseline_run_id`` by name, and an approved manifest is otherwise not visible
over the API. This listing lets a dashboard find those IDs without reading the
manifest file.

Only approved runs appear, and only when they are stored, so every ID listed
works with the incident endpoints. The route is registered only when
``AEGIS_DEMO_API_ENABLED`` is true, and the settings refuse that in production.
The listing reads the trusted-baseline registry and the store. It changes
neither, and it starts no simulation.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.domain.telemetry import RunId, ScenarioId
from app.routers.dependencies import get_event_store, get_trusted_baselines
from app.services.baseline_trust import TrustedBaselines
from app.services.event_store import EventStore

router = APIRouter(prefix="/api/v1", tags=["demo"])


class DemoBaseline(BaseModel):
    """One approved baseline run that is stored and can be named in a request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    scenario_id: ScenarioId
    seed: int
    ticks: Annotated[int, Field(ge=1)]
    event_count: Annotated[int, Field(ge=0)]
    approval_method: str


class DemoBaselineList(BaseModel):
    """Response body for GET /api/v1/demo/baselines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_origin: Literal["synthetic"]
    items: list[DemoBaseline]
    total: Annotated[int, Field(ge=0)]


@router.get(
    "/demo/baselines",
    response_model=DemoBaselineList,
    summary="List approved synthetic baseline runs that are stored",
)
def list_demo_baselines(
    store: Annotated[EventStore, Depends(get_event_store)],
    trusted: Annotated[TrustedBaselines, Depends(get_trusted_baselines)],
) -> DemoBaselineList:
    """Return approved baselines that are in the store, ordered by run ID.

    An empty list means no approved baseline has been stored yet. It is not an
    error. An approved baseline that is not stored is left out, because the
    incident endpoints would refuse it with a 404.
    """
    stored = {run.run_id: run for run in store.runs()}
    items = [
        DemoBaseline(
            run_id=approval.run_id,
            scenario_id=approval.spec.scenario,
            seed=approval.spec.seed,
            ticks=approval.spec.ticks,
            event_count=len(stored[approval.run_id].events),
            approval_method=approval.method,
        )
        for approval in sorted(trusted.approvals, key=lambda item: item.run_id)
        if approval.run_id in stored
    ]
    return DemoBaselineList(
        data_origin="synthetic",
        items=items,
        total=len(items),
    )
