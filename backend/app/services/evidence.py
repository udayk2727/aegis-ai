"""Read-only evidence collection and telemetry gap detection for one run.

Evidence is never fabricated. Every record points at an event that is in the
store, and its value, timestamp, and severity are copied from that event. The
only thing added is the detector's comparison with its baseline.

Scope. An incident's evidence is the anomalous observations of the series that
took part in its alerts, from ``window`` before its first alert to ``window``
after its last alert. Each observation gets one inclusion reason:

- ``contributed_to_alert`` when its tick was an alert tick (a qualifying detection);
- ``pre_incident_context`` when it is before the first qualifying detection;
- ``anomalous_in_window`` otherwise (an anomalous measurement after onset that did
  not qualify an alert).

Only the first reason shows degradation. The others are kept for context and are
never used for observation times or the timeline.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from app.domain.incident_records import (
    BaselineComparison,
    EvidenceRecord,
    InclusionReason,
    TelemetryGap,
)
from app.domain.telemetry import TelemetryEvent
from app.services.analytics import SeriesKey
from app.services.detection import AnomalyDetector, Prediction


def find_telemetry_gaps(
    events: Iterable[TelemetryEvent],
    tick_interval_seconds: int,
) -> list[TelemetryGap]:
    """Return every pair of consecutive observations further apart than one tick.

    Timestamps are sorted first, so the order events were stored in does not
    matter, and duplicate timestamps count once.
    """
    interval = timedelta(seconds=tick_interval_seconds)
    timestamps = sorted({event.timestamp for event in events})
    return [
        TelemetryGap(observed_before=before, observed_after=after)
        for before, after in pairwise(timestamps)
        if after - before > interval
    ]


def collect_evidence(
    *,
    series: frozenset[SeriesKey],
    alert_ticks: frozenset[datetime],
    span_start: datetime,
    span_end: datetime,
    first_detection_at: datetime,
    anomalies: Mapping[datetime, Mapping[SeriesKey, Prediction]],
    events_by_id: Mapping[str, TelemetryEvent],
    detector: AnomalyDetector,
    training_run_ids: Sequence[str],
) -> list[EvidenceRecord]:
    """Return the anomalous observations of ``series`` inside the span, oldest first.

    ``anomalies`` must hold only anomalous predictions, indexed by tick. Each
    prediction names the event it scored, so the value is read from that event.
    ``first_detection_at`` is the first alert tick of the incident. It separates
    pre-incident context from anomalies that came after onset.
    """
    records: list[EvidenceRecord] = []
    for tick in sorted(anomalies):
        if not span_start <= tick <= span_end:
            continue
        for key in sorted(anomalies[tick]):
            if key not in series:
                continue
            prediction = anomalies[tick][key]
            event = events_by_id[prediction.event_id]
            records.append(
                EvidenceRecord(
                    source_event_id=event.event_id,
                    run_id=event.run_id,
                    service=event.service,
                    metric_name=event.metric_name,
                    unit=event.unit,
                    severity=event.severity,
                    observed_at=event.timestamp,
                    observed_value=event.value,
                    inclusion_reason=_inclusion_reason(
                        tick, alert_ticks, first_detection_at
                    ),
                    baseline_comparison=_baseline_comparison(
                        prediction, detector, training_run_ids
                    ),
                )
            )
    return records


def _inclusion_reason(
    tick: datetime,
    alert_ticks: frozenset[datetime],
    first_detection_at: datetime,
) -> InclusionReason:
    """Classify one anomalous observation as a detection, measurement, or context."""
    if tick in alert_ticks:
        return InclusionReason.CONTRIBUTED_TO_ALERT
    if tick < first_detection_at:
        return InclusionReason.PRE_INCIDENT_CONTEXT
    return InclusionReason.ANOMALOUS_IN_WINDOW


def _baseline_comparison(
    prediction: Prediction,
    detector: AnomalyDetector,
    training_run_ids: Sequence[str],
) -> BaselineComparison | None:
    """Describe the detector's comparison, or None when no finite score exists.

    A zero-spread deviation is anomalous without a score, so there is nothing
    to compare numerically and the field stays empty rather than inventing one.
    """
    if prediction.score is None:
        return None
    return BaselineComparison(
        detector=detector.name,
        direction=detector.config.direction.value,
        threshold=detector.config.threshold,
        score=prediction.score,
        training_run_ids=tuple(sorted(training_run_ids)),
    )
