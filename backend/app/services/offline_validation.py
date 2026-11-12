"""Offline, label-aware validation of candidate baseline runs.

This module reads ground-truth labels, so it sits outside the detection path.
Detection, correlation, the incident pipeline, and the routers never import it
for a request. The only runtime caller is ``trusted_baselines``, which runs the
check when a manifest is loaded at startup, before any request is served.

A synthetic run can be checked against its labels because the simulator knows
which events it injected a fault into. That check proves only that the run
contains no injected fault. It does not prove that the run is representative of
production traffic. See docs/architecture.md, section 13.12.
"""

from collections.abc import Iterable

from app.domain.telemetry import ScenarioId
from app.services.baseline_trust import BaselineApproval
from app.services.simulation import SimulationRun, SimulationSpec, run_simulation

SYNTHETIC_GROUND_TRUTH = "synthetic_ground_truth"


class BaselineRejected(ValueError):
    """A candidate run contains injected faults and cannot define normal behaviour."""


def validate_synthetic_baseline(run: SimulationRun) -> BaselineApproval:
    """Approve a simulated run only if it has no fault scenario and no anomalous label.

    Both checks run. The scenario check catches a fault run even if its labels
    were lost. The label check catches any injected anomaly, so a run cannot
    pass on its scenario alone. Raises BaselineRejected otherwise.
    """
    if run.spec.scenario is not ScenarioId.NORMAL_OPERATION:
        raise BaselineRejected(
            f"run {run.run_id} uses fault scenario {run.spec.scenario.value}"
        )
    if run.anomaly_window is not None or run.anomalous_event_count:
        raise BaselineRejected(f"run {run.run_id} contains injected anomalies")
    return BaselineApproval(
        run_id=run.run_id,
        spec=run.spec,
        method=SYNTHETIC_GROUND_TRUTH,
        checked_event_count=len(run.events),
    )


def approve_baseline_specs(specs: Iterable[SimulationSpec]) -> list[BaselineApproval]:
    """Simulate each spec and approve it, or raise on the first faulty one.

    Simulation is deterministic, so the run checked here is the run a later
    request will see when it is stored from the same spec.
    """
    return [validate_synthetic_baseline(run_simulation(spec)) for spec in specs]
