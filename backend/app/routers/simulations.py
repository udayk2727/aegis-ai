"""Simulation endpoint: start a deterministic telemetry run.

POST /api/v1/simulations generates a run and stores its events. The response
is a summary with the ground-truth fault window, not the events themselves;
read events through GET /api/v1/events. Repeating an identical request is
safe: it returns 200 with the same summary and stores nothing new.

This endpoint is unauthenticated and is for local development only. It changes
server state, so it must not be exposed on a shared, staging, or production
host until the authentication design in docs/architecture.md is accepted.
"""

from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from app.domain.telemetry import RunId, ScenarioId, UtcTimestamp
from app.routers.dependencies import get_event_store
from app.services.event_store import EventStore, RunTooLargeError
from app.services.simulation import (
    AnomalyWindow,
    SimulationRun,
    SimulationSpec,
    run_simulation,
)

router = APIRouter(prefix="/api/v1", tags=["simulations"])


class SimulationRunSummary(BaseModel):
    """Response body for POST /api/v1/simulations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    scenario_id: ScenarioId
    seed: int
    ticks: int
    tick_interval_seconds: int
    start_time: UtcTimestamp
    event_count: int
    anomalous_event_count: int
    anomaly_window: AnomalyWindow | None

    @classmethod
    def from_run(cls, run: SimulationRun) -> Self:
        """Summarize a run without including its events."""
        return cls(
            run_id=run.run_id,
            scenario_id=run.spec.scenario,
            seed=run.spec.seed,
            ticks=run.spec.ticks,
            tick_interval_seconds=run.spec.tick_interval_seconds,
            start_time=run.spec.start_time,
            event_count=len(run.events),
            anomalous_event_count=run.anomalous_event_count,
            anomaly_window=run.anomaly_window,
        )


class RunTooLargeDetail(BaseModel):
    """Structured 422 body when a run cannot fit in the event store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["run_exceeds_capacity"]
    message: str
    run_event_count: int


@router.post(
    "/simulations",
    response_model=SimulationRunSummary,
    status_code=201,
    responses={
        200: {"description": "Identical run already stored; nothing added."},
        422: {"description": "The run alone exceeds the event store capacity."},
    },
)
def create_simulation(
    spec: SimulationSpec,
    response: Response,
    store: Annotated[EventStore, Depends(get_event_store)],
) -> SimulationRunSummary:
    """Run a simulation and store its events, or confirm an identical run exists.

    A run larger than the whole store is refused with a structured 422 and
    nothing is stored. The store's capacity is not disclosed in the response.
    """
    run = run_simulation(spec)
    try:
        stored = store.add_run(run)
    except RunTooLargeError as error:
        detail = RunTooLargeDetail(
            code="run_exceeds_capacity",
            message="the simulation run is larger than the event store can hold",
            run_event_count=error.event_count,
        )
        raise HTTPException(status_code=422, detail=detail.model_dump()) from error
    if not stored:
        response.status_code = 200
    return SimulationRunSummary.from_run(run)
