"""Deterministic investigation reports for the agent tests.

Each report is built from the simulator with the same baseline and evaluation
seeds as the other incident tests. Results are cached, because a simulation is
the expensive step, and the cached values are never modified.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from app.domain.investigation_records import Investigation
from app.domain.telemetry import ScenarioId, TelemetryEvent
from app.services.incident_pipeline import build_incidents, score_run
from app.services.investigation import investigate
from tests.incident_factories import (
    EVALUATION_SEED,
    baseline_runs,
    observe,
    simulate,
    trusted_baselines,
)


@dataclass(frozen=True)
class FaultFixture:
    """One report and the events of its source run, which citations resolve to."""

    report: Investigation
    events_by_id: Mapping[str, TelemetryEvent]


@lru_cache(maxsize=None)
def fault_fixture(
    scenario: ScenarioId,
    seed: int = EVALUATION_SEED,
) -> FaultFixture:
    """Build the report for the first incident of one fault run."""
    baselines = baseline_runs()
    baseline_ids = [run.run_id for run in baselines]
    fault = simulate(scenario, seed)
    runs = [observe(run) for run in [*baselines, fault]]
    trusted = trusted_baselines()
    incident = next(
        item
        for item in build_incidents(runs, baseline_ids, trusted=trusted)
        if item.source_run_id == fault.run_id
    )
    report = investigate(
        incident,
        score_run(runs, baseline_ids, fault.run_id, trusted=trusted),
    )
    return FaultFixture(
        report=report,
        events_by_id={event.event_id: event for event in fault.events},
    )
