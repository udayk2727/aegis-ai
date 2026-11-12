"""Offline evaluation of investigation reports against synthetic ground truth.

This module reads ground-truth labels, so it sits outside the request path. The
investigation it scores never sees the labels, and this module never changes
what the investigation computes.

It checks these properties:

- Provenance. Every cited reference resolves to a stored event of the source
  run, on the named series, inside the span. The check is the same function the
  API runs before it returns a report (``investigation_provenance``).
- Support. Each hypothesis cites at least one observation that is an anomaly or
  an onset, not only a normal measurement.
- Reproducibility. Rebuilding the incidents and reports from the same runs gives
  identical reports.
- Window agreement. Each incident is classed as in the labelled window when its
  alert ticks overlap the window the simulator labelled anomalous. An incident
  that does not overlap it is a false positive. The detector is not tuned on
  this count.
- Scenario agreement. Each fault run is checked for whether the hypothesis that
  matches its simulator fault is generated, and whether it ranks first.

Scenario agreement is not causal accuracy. The simulator's fault type is a
label, not a verified cause. A report that names the matching pattern agrees
with the simulator. That agreement is evidence the rules fire on the designed
signals. It is not evidence the rules find the cause of a real incident.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.incident_records import Incident
from app.domain.investigation_records import (
    HypothesisKind,
    Investigation,
    ObservationKind,
)
from app.domain.telemetry import ScenarioId, TelemetryEvent
from app.services.baseline_trust import TrustedBaselines
from app.services.incident_pipeline import ObservedRun, build_incidents, score_run
from app.services.investigation import investigate
from app.services.investigation_provenance import provenance_violations
from app.services.offline_validation import validate_synthetic_baseline
from app.services.simulation import SimulationRun

EXPECTED_HYPOTHESIS: dict[ScenarioId, HypothesisKind] = {
    ScenarioId.DATABASE_LATENCY_SPIKE: (
        HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION
    ),
    ScenarioId.CPU_SATURATION: HypothesisKind.CPU_RESOURCE_SATURATION,
    ScenarioId.API_ERRORS: HypothesisKind.API_ERROR_PROPAGATION,
    ScenarioId.CONNECTION_FAILURE: HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,
}


@dataclass(frozen=True)
class RunScore:
    """Evaluation outcome for one simulated run and all of its investigations.

    ``incident_count`` is the total. It equals ``window_incidents`` plus
    ``false_positive_incidents``. A normal run has no labelled window, so every
    incident it produces is a false positive.
    """

    run_id: str
    scenario: ScenarioId
    incident_count: int
    window_incidents: int
    false_positive_incidents: int
    provenance_failures: int
    unsupported_hypotheses: int
    reproducible: bool
    expected_present: bool | None
    expected_ranked_first: bool | None
    contradictions: int
    missing_items: int


def evaluate_investigations(
    baselines: Sequence[SimulationRun],
    evaluation: Sequence[SimulationRun],
) -> list[RunScore]:
    """Score every incident in the evaluation runs, in the order of ``evaluation``.

    Every baseline is checked with the offline synthetic validation first, and a
    faulty baseline raises BaselineRejected. Each run's incidents are built twice,
    from fresh inputs, to test reproducibility.
    """
    trusted = TrustedBaselines(
        tuple(validate_synthetic_baseline(run) for run in baselines)
    )
    observed = [
        ObservedRun(run.run_id, run.spec.tick_interval_seconds, run.events)
        for run in [*baselines, *evaluation]
    ]
    baseline_ids = [run.run_id for run in baselines]
    first = _reports_by_run(observed, baseline_ids, trusted)
    second = _reports_by_run(observed, baseline_ids, trusted)

    scores: list[RunScore] = []
    for run in evaluation:
        pairs = first.get(run.run_id, [])
        reports = [report for _, report in pairs]
        events_by_id = {event.event_id: event for event in run.events}
        window = labelled_window(run.events)
        in_window = sum(overlaps(incident, window) for incident, _ in pairs)
        expected = EXPECTED_HYPOTHESIS.get(run.spec.scenario)
        scores.append(
            RunScore(
                run_id=run.run_id,
                scenario=run.spec.scenario,
                incident_count=len(reports),
                window_incidents=in_window,
                false_positive_incidents=len(reports) - in_window,
                provenance_failures=sum(
                    provenance_failures(report, events_by_id) for report in reports
                ),
                unsupported_hypotheses=sum(
                    unsupported_hypotheses(report) for report in reports
                ),
                reproducible=reports
                == [report for _, report in second.get(run.run_id, [])],
                expected_present=(
                    None
                    if expected is None
                    else any(
                        hypothesis.kind is expected
                        for report in reports
                        for hypothesis in report.hypotheses
                    )
                ),
                expected_ranked_first=(
                    None
                    if expected is None
                    else any(
                        report.hypotheses[0].kind is expected
                        for report in reports
                        if report.hypotheses
                    )
                ),
                contradictions=sum(
                    len(report.contradictory_evidence) for report in reports
                ),
                missing_items=sum(len(report.missing_evidence) for report in reports),
            )
        )
    return scores


def labelled_window(
    events: Sequence[TelemetryEvent],
) -> tuple[datetime, datetime] | None:
    """Return the first and last anomalous-labelled timestamps, or None if none.

    This reads labels, so it belongs here and not in the request path.
    """
    times = [event.timestamp for event in events if event.label.is_anomalous]
    if not times:
        return None
    return min(times), max(times)


def overlaps(
    incident: Incident,
    window: tuple[datetime, datetime] | None,
) -> bool:
    """True when the incident's alert ticks reach into the labelled window."""
    if window is None:
        return False
    start, end = window
    return incident.first_detection_at <= end and start <= incident.latest_detection_at


def provenance_failures(
    report: Investigation,
    events_by_id: dict[str, TelemetryEvent],
) -> int:
    """Count the provenance violations in one report. Zero means all references hold."""
    return len(provenance_violations(report, events_by_id))


def unsupported_hypotheses(report: Investigation) -> int:
    """Count hypotheses whose support is only normal measurements."""
    kinds = {item.observation_id: item.kind for item in report.verified_observations}
    return sum(
        1
        for hypothesis in report.hypotheses
        if not any(
            kinds[observation_id] is not ObservationKind.NORMAL_SERIES
            for observation_id in hypothesis.supporting_observation_ids
        )
    )


def _reports_by_run(
    observed: Sequence[ObservedRun],
    baseline_ids: Sequence[str],
    trusted: TrustedBaselines,
) -> dict[str, list[tuple[Incident, Investigation]]]:
    """Build every incident's report for the non-baseline runs, grouped by run."""
    incidents = build_incidents(observed, baseline_ids, trusted=trusted)
    pairs: dict[str, list[tuple[Incident, Investigation]]] = {}
    verdicts: dict[str, tuple] = {}
    for incident in incidents:
        run_id = incident.source_run_id
        if run_id not in verdicts:
            verdicts[run_id] = score_run(
                observed,
                baseline_ids,
                run_id,
                trusted=trusted,
            )
        pairs.setdefault(run_id, []).append(
            (incident, investigate(incident, verdicts[run_id]))
        )
    return pairs
