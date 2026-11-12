"""Offline scoring of the correlation window against synthetic ground truth.

This module reads ground-truth labels to decide whether each incident matched a
real injected fault. It is an evaluation tool, not a detection feature. The
pipeline it scores never sees the labels, and this module never changes what
the pipeline computes.

Every baseline is checked with the offline synthetic validation before scoring,
so a faulty baseline stops the sweep rather than skewing it.

Definitions, for one window and one run:

- A missed fault is a fault run with no incident that alerts inside its fault
  window.
- A false split is each incident beyond the first one that alerts inside the
  same fault window. The fault was cut into more than one incident.
- A false merge is an incident that mixes alerts inside the fault window with
  alerts outside it. Unrelated noise was joined to the fault.
- A false incident is any incident with no alert inside the fault window. Every
  incident in a normal run is one.

The simulator puts at most one fault in each run, so it cannot produce a merge
of two real faults. That case is covered by unit tests only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.incident_records import Incident, InclusionReason
from app.domain.telemetry import ScenarioId
from app.services.baseline_trust import TrustedBaselines
from app.services.correlation import CorrelationConfig
from app.services.incident_pipeline import ObservedRun, build_incidents
from app.services.offline_validation import validate_synthetic_baseline
from app.services.simulation import SimulationRun


@dataclass(frozen=True)
class WindowScore:
    """Counts of each error type for one correlation window over the given runs."""

    window_seconds: int
    fault_runs: int
    normal_runs: int
    incidents: int
    missed_faults: int
    false_splits: int
    false_merges: int
    false_incidents: int


def score_windows(
    baselines: Sequence[SimulationRun],
    evaluation: Sequence[SimulationRun],
    windows: Sequence[int],
) -> list[WindowScore]:
    """Score each window on the evaluation runs, using the given baselines.

    Raises BaselineRejected if any baseline contains an injected fault. The
    result is in the order of ``windows``. Each run is scored on its own, so the
    result does not depend on the order the runs were supplied in.
    """
    trusted = TrustedBaselines(
        tuple(validate_synthetic_baseline(run) for run in baselines)
    )
    observed = [
        ObservedRun(run.run_id, run.spec.tick_interval_seconds, run.events)
        for run in [*baselines, *evaluation]
    ]
    baseline_ids = [run.run_id for run in baselines]
    truth = {run.run_id: _fault_ticks(run) for run in evaluation}
    fault_runs = sum(1 for run in evaluation if _is_fault(run))

    scores: list[WindowScore] = []
    for window in windows:
        incidents = build_incidents(
            observed,
            baseline_ids,
            trusted=trusted,
            correlation=CorrelationConfig(window_seconds=window),
        )
        scores.append(
            _score(
                window=window,
                fault_runs=fault_runs,
                normal_runs=len(evaluation) - fault_runs,
                incidents=incidents,
                truth=truth,
                evaluation=evaluation,
            )
        )
    return scores


def _score(
    *,
    window: int,
    fault_runs: int,
    normal_runs: int,
    incidents: Sequence[Incident],
    truth: dict[str, tuple[bool, set[datetime]]],
    evaluation: Sequence[SimulationRun],
) -> WindowScore:
    """Tally errors for one window from its incidents and the labelled truth."""
    by_run: dict[str, list[Incident]] = {}
    for incident in incidents:
        by_run.setdefault(incident.source_run_id, []).append(incident)

    missed = splits = merges = false = 0
    for run in evaluation:
        is_fault, fault_ticks = truth[run.run_id]
        found = by_run.get(run.run_id, [])
        in_fault: list[Incident] = []
        for incident in found:
            alerts = _alert_ticks(incident)
            hits = alerts & fault_ticks
            noise = alerts - fault_ticks
            if hits:
                in_fault.append(incident)
            if hits and noise:
                merges += 1
            if not hits:
                false += 1
        if is_fault and not in_fault:
            missed += 1
        if is_fault and len(in_fault) > 1:
            splits += len(in_fault) - 1
    return WindowScore(
        window_seconds=window,
        fault_runs=fault_runs,
        normal_runs=normal_runs,
        incidents=len(incidents),
        missed_faults=missed,
        false_splits=splits,
        false_merges=merges,
        false_incidents=false,
    )


def _is_fault(run: SimulationRun) -> bool:
    """True when the run was generated from a fault scenario."""
    return run.spec.scenario is not ScenarioId.NORMAL_OPERATION


def _fault_ticks(run: SimulationRun) -> tuple[bool, set[datetime]]:
    """Return whether the run is a fault run, and the timestamps of its anomalies."""
    return _is_fault(run), {
        event.timestamp for event in run.events if event.label.is_anomalous
    }


def _alert_ticks(incident: Incident) -> set[datetime]:
    """Timestamps at which the incident's rule fired, from its qualifying evidence."""
    return {
        record.observed_at
        for record in incident.evidence
        if record.inclusion_reason is InclusionReason.CONTRIBUTED_TO_ALERT
    }
