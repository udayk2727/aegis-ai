"""Offline incident pipeline: stored runs in, validated incident records out.

This module consumes the existing detection and rule services and performs no
I/O. It takes runs as plain values, so the same inputs always give the same
incidents, and the API layer only has to read the store and call it.

Method, in order:

1. Fit one detector on the baseline runs. They are supplied by the caller and
   used as normal behaviour. Their events are not filtered by label. The API
   accepts only runs on the trusted list (see architecture 13.12).
2. Score every other run against that fitted model. A baseline run is never
   scored, so the model is never tested on its own training data.
3. Apply the Day 4 incident rule to each run's predictions to get alert ticks.
4. Group alerts within each run only (see ``correlation``).
5. Attach evidence, telemetry gaps, a timeline, and a deterministic ID to each
   group.

The pipeline never reads ``label``. Ground-truth labels do not reach a
prediction, a grouping, an ID, or an evidence record. A test flips every label
and checks that the incidents do not change.
"""

import hashlib
import json
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.domain.incident_records import (
    Incident,
    IncidentStatus,
    InclusionReason,
    RootCauseAssessment,
    TelemetryGap,
)
from app.domain.telemetry import TelemetryEvent
from app.services.analytics import SeriesKey
from app.services.baseline_trust import TrustedBaselines
from app.services.correlation import (
    Alert,
    CorrelationConfig,
    anomalies_by_tick,
    build_alerts,
    correlation_rationale,
    group_alerts,
)
from app.services.detection import AnomalyDetector, Direction, Prediction
from app.services.detectors import ZScoreConfig, ZScoreDetector
from app.services.evidence import collect_evidence, find_telemetry_gaps
from app.services.incident import IncidentRule
from app.services.timeline import build_timeline
from app.services.topology import DEFAULT_TOPOLOGY, ServiceTopology

# The Day 4 validation winner (see docs/architecture.md, section 12). Changing
# either value changes every incident, so it is kept here and not in callers.
SELECTED_RULE = IncidentRule(min_affected_series=3, min_consecutive_ticks=1)
SELECTED_DETECTOR_CONFIG = ZScoreConfig(direction=Direction.UPPER_TAIL)


def selected_detector() -> AnomalyDetector:
    """Return an unfitted detector with the Day 4 selected configuration."""
    return ZScoreDetector(SELECTED_DETECTOR_CONFIG)


@dataclass(frozen=True)
class ObservedRun:
    """One simulation run as the pipeline sees it: its ID, cadence, and events."""

    run_id: str
    tick_interval_seconds: int
    events: tuple[TelemetryEvent, ...]


@dataclass(frozen=True)
class _Scoring:
    """Shared inputs for building incidents from one run's predictions."""

    run: ObservedRun
    detector: AnomalyDetector
    baseline_run_ids: tuple[str, ...]
    rule: IncidentRule
    correlation: CorrelationConfig
    topology: ServiceTopology


def build_incidents(
    runs: Sequence[ObservedRun],
    baseline_run_ids: Collection[str],
    *,
    trusted: TrustedBaselines,
    detector_factory: Callable[[], AnomalyDetector] = selected_detector,
    rule: IncidentRule = SELECTED_RULE,
    correlation: CorrelationConfig = CorrelationConfig(),
    topology: ServiceTopology = DEFAULT_TOPOLOGY,
) -> list[Incident]:
    """Build every incident from the non-baseline runs, ordered by first detection.

    ``trusted`` is required and has no default, so a caller must state which
    baselines it trusts. Every baseline must be approved in it. A run that is
    not approved is refused even when it is stored and well formed. The check
    does not read labels. The approval itself is established offline.

    Raises ValueError when no baseline is given, a baseline is not among
    ``runs``, a baseline is not approved, or a run repeats an event ID. The last
    case matters because evidence reads each value by event ID, so a repeated ID
    could copy the value of the wrong event.
    """
    baseline_ids = tuple(sorted(set(baseline_run_ids)))
    if not baseline_ids:
        raise ValueError("at least one baseline run is required")
    known_ids = {run.run_id for run in runs}
    unknown = [run_id for run_id in baseline_ids if run_id not in known_ids]
    if unknown:
        raise ValueError(f"baseline run not found: {unknown[0]}")
    unapproved = [run_id for run_id in baseline_ids if not trusted.is_approved(run_id)]
    if unapproved:
        raise ValueError(f"baseline run is not approved: {unapproved[0]}")
    for run in runs:
        if len({event.event_id for event in run.events}) != len(run.events):
            raise ValueError(f"run {run.run_id} repeats an event ID")

    baseline_events = [
        event
        for run in runs
        if run.run_id in baseline_ids
        for event in run.events
    ]
    detector = detector_factory()
    detector.fit(baseline_events)

    incidents: list[Incident] = []
    for run in sorted(runs, key=lambda item: item.run_id):
        if run.run_id in baseline_ids:
            continue
        scoring = _Scoring(
            run=run,
            detector=detector,
            baseline_run_ids=baseline_ids,
            rule=rule,
            correlation=correlation,
            topology=topology,
        )
        incidents.extend(_run_incidents(scoring))
    return sorted(
        incidents,
        key=lambda item: (item.first_detection_at, item.incident_id),
    )


def _run_incidents(scoring: _Scoring) -> list[Incident]:
    """Score one run and return one incident per correlated group of its alerts."""
    run = scoring.run
    predictions = scoring.detector.predict(run.events)
    anomalies = anomalies_by_tick(predictions)
    run_rule = replace(scoring.rule, tick_interval_seconds=run.tick_interval_seconds)
    alerts = build_alerts(run_rule.alert_times(predictions), anomalies)
    groups = group_alerts(
        alerts,
        scoring.topology,
        scoring.correlation.window_seconds,
    )
    events_by_id = {event.event_id: event for event in run.events}
    gaps = find_telemetry_gaps(run.events, run.tick_interval_seconds)
    return [
        _incident(
            group,
            scoring=scoring,
            anomalies=anomalies,
            events_by_id=events_by_id,
            gaps=gaps,
        )
        for group in groups
    ]


def _incident(
    group: Sequence[Alert],
    *,
    scoring: _Scoring,
    anomalies: dict[datetime, dict[SeriesKey, Prediction]],
    events_by_id: dict[str, TelemetryEvent],
    gaps: Sequence[TelemetryGap],
) -> Incident:
    """Assemble the incident for one group of alerts from one run."""
    run = scoring.run
    window = timedelta(seconds=scoring.correlation.window_seconds)
    first_detection = group[0].timestamp
    latest_detection = group[-1].timestamp
    series = frozenset(key for alert in group for key in alert.keys)
    span_start = first_detection - window
    span_end = latest_detection + window

    evidence = collect_evidence(
        series=series,
        alert_ticks=frozenset(alert.timestamp for alert in group),
        span_start=span_start,
        span_end=span_end,
        first_detection_at=first_detection,
        anomalies=anomalies,
        events_by_id=events_by_id,
        detector=scoring.detector,
        training_run_ids=scoring.baseline_run_ids,
    )
    if not evidence:
        raise RuntimeError("an alert group produced no evidence")
    # Only observations taken at an alert tick are qualifying detections. Other
    # anomalies in the span are context or non-qualifying measurements. They stay
    # in ``evidence`` but never set the observation times or the timeline.
    degradation = [
        record
        for record in evidence
        if record.inclusion_reason is InclusionReason.CONTRIBUTED_TO_ALERT
    ]
    if not degradation:
        raise RuntimeError("an alert group produced no contributing evidence")

    span_gaps = [
        gap
        for gap in gaps
        if gap.observed_after >= span_start and gap.observed_before <= span_end
    ]
    services = sorted({key.service for key in series})
    metrics = sorted({key.metric_name for key in series})
    rule_name = scoring.rule.name
    incident_id = _incident_id(
        run.run_id,
        scoring.detector.name,
        rule_name,
        scoring.baseline_run_ids,
        first_detection,
        sorted(series),
    )
    return Incident(
        incident_id=incident_id,
        status=IncidentStatus.DETECTED,
        root_cause_assessment=RootCauseAssessment.NOT_ASSESSED,
        source_run_id=run.run_id,
        baseline_run_ids=scoring.baseline_run_ids,
        detector=scoring.detector.name,
        rule=rule_name,
        correlation_window_seconds=scoring.correlation.window_seconds,
        first_observation_at=degradation[0].observed_at,
        first_detection_at=first_detection,
        latest_detection_at=latest_detection,
        latest_observation_at=degradation[-1].observed_at,
        alert_count=len(group),
        affected_services=tuple(services),
        affected_metrics=tuple(metrics),
        correlation_rationale=correlation_rationale(
            group,
            scoring.topology,
            scoring.correlation.window_seconds,
        ),
        evidence=tuple(evidence),
        timeline=tuple(build_timeline(degradation, first_detection, span_gaps)),
        telemetry_gaps=tuple(span_gaps),
    )


def _incident_id(
    run_id: str,
    detector: str,
    rule: str,
    baseline_run_ids: Sequence[str],
    first_detection: datetime,
    series: Sequence[SeriesKey],
) -> str:
    """Derive a stable ID from the inputs that define the incident.

    The same run, detector, rule, baseline, first alert, and series always give
    the same ID, so an incident keeps its identity across requests.
    """
    payload = json.dumps(
        {
            "run_id": run_id,
            "detector": detector,
            "rule": rule,
            "baseline_run_ids": list(baseline_run_ids),
            "first_detection_at": first_detection.isoformat(),
            "series": [
                [key.service, str(key.metric_name), str(key.unit)] for key in series
            ],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"inc-{digest[:16]}"
