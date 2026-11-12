"""Verified observations: facts read from stored events, with no interpretation.

Three kinds, and each cites the stored event IDs it was computed from:

- ``anomalous_series``: a series with at least one measurement that contributed
  to an alert. Context samples before onset do not count.
- ``normal_series``: a watched series with normal verdicts in the analysis span,
  and no measurement that contributed to an alert in that span.
- ``onset_order``: the first alert-contributing measurement of two series, in
  the order they occurred.

Inputs are the incident's evidence and the detector's verdicts for the source
run. Labels are never read, so every observation can be checked against the
store.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.domain.incident_records import EvidenceRecord, Incident, InclusionReason
from app.domain.investigation_records import (
    ObservationId,
    ObservationKind,
    SeriesRef,
    VerifiedObservation,
)
from app.services.detection import Decision, Prediction


def analysis_span(incident: Incident) -> tuple[datetime, datetime]:
    """Return the span the incident's evidence was collected from, inclusive."""
    window = timedelta(seconds=incident.correlation_window_seconds)
    return (
        incident.first_detection_at - window,
        incident.latest_detection_at + window,
    )


def series_of_prediction(prediction: Prediction) -> SeriesRef:
    """Return the series a prediction scored."""
    return SeriesRef(
        service=prediction.key.service,
        metric_name=prediction.key.metric_name,
    )


def sort_key(series: SeriesRef) -> tuple[str, str]:
    """Order series by service, then metric, so output ignores input order."""
    return series.service, series.metric_name.value


def build_observations(
    incident: Incident,
    predictions: Sequence[Prediction],
    *,
    watched: Sequence[SeriesRef],
    orderings: Sequence[tuple[SeriesRef, SeriesRef]],
) -> list[VerifiedObservation]:
    """Return the verified observations for one incident, in a fixed order.

    ``watched`` lists the series that normal observations may be recorded for.
    ``orderings`` lists the pairs whose onset order is recorded. Predictions must
    cover the whole source run. Each contributing series is part of the incident
    (every series at an alert tick joins the group), so reading contributions
    from the incident's evidence is complete for the span.
    """
    span_start, span_end = analysis_span(incident)
    contributing = _contributing_by_series(incident.evidence)
    excluded = _excluded_by_series(incident.evidence)
    observations = [
        _anomalous(series, records, excluded.get(series, 0))
        for series, records in sorted(
            contributing.items(), key=lambda item: sort_key(item[0])
        )
    ]

    in_span = [
        prediction
        for prediction in predictions
        if span_start <= prediction.timestamp <= span_end
    ]
    for series in sorted(set(watched), key=sort_key):
        if series in contributing:
            continue
        normal = [
            prediction
            for prediction in in_span
            if prediction.decision is Decision.NORMAL
            and series_of_prediction(prediction) == series
        ]
        if normal:
            observations.append(_normal(series, normal))

    for earlier, later in orderings:
        if earlier in contributing and later in contributing:
            observations.append(
                _onset(earlier, contributing[earlier][0], later, contributing[later][0])
            )
    return observations


def observation_id(kind: ObservationKind, series: Sequence[SeriesRef]) -> ObservationId:
    """Name an observation from its kind and series, so the name is stable."""
    parts = "-".join(f"{item.service}-{item.metric_name.value}" for item in series)
    return f"obs-{kind.value}-{parts}"


def _contributing_by_series(
    evidence: Sequence[EvidenceRecord],
) -> dict[SeriesRef, list[EvidenceRecord]]:
    """Group alert-contributing evidence by series, oldest first."""
    grouped: dict[SeriesRef, list[EvidenceRecord]] = {}
    for record in evidence:
        if record.inclusion_reason is not InclusionReason.CONTRIBUTED_TO_ALERT:
            continue
        series = SeriesRef(service=record.service, metric_name=record.metric_name)
        grouped.setdefault(series, []).append(record)
    for records in grouped.values():
        records.sort(key=lambda record: record.observed_at)
    return grouped


def _excluded_by_series(
    evidence: Sequence[EvidenceRecord],
) -> dict[SeriesRef, int]:
    """Count the anomalous samples of each series that did not contribute to alerts."""
    counts: dict[SeriesRef, int] = {}
    for record in evidence:
        if record.inclusion_reason is InclusionReason.CONTRIBUTED_TO_ALERT:
            continue
        series = SeriesRef(service=record.service, metric_name=record.metric_name)
        counts[series] = counts.get(series, 0) + 1
    return counts


def _anomalous(
    series: SeriesRef,
    records: Sequence[EvidenceRecord],
    excluded: int,
) -> VerifiedObservation:
    """State that a series was anomalous at alert ticks, with its values and scores.

    ``excluded`` counts the other anomalous samples in the span. They are stated
    here so the exclusion is visible, and they never enter the observation's
    events or times.
    """
    values = [record.observed_value for record in records]
    unit = records[0].unit.value
    statement = (
        f"{series.service} {series.metric_name.value} was anomalous at "
        f"{len(records)} alert tick(s) from {records[0].observed_at.isoformat()} "
        f"to {records[-1].observed_at.isoformat()}, with values from "
        f"{min(values):g} to {max(values):g} {unit}."
    )
    comparisons = [
        record.baseline_comparison
        for record in records
        if record.baseline_comparison is not None
    ]
    if comparisons:
        scores = [comparison.score for comparison in comparisons]
        statement += (
            f" Detector scores ranged from {min(scores):.2f} to {max(scores):.2f} "
            f"against a threshold of {comparisons[0].threshold:g}."
        )
    if excluded:
        statement += (
            f" {excluded} other anomalous sample(s) in the span did not contribute "
            "to an alert and are not counted."
        )
    return VerifiedObservation(
        observation_id=observation_id(ObservationKind.ANOMALOUS_SERIES, [series]),
        kind=ObservationKind.ANOMALOUS_SERIES,
        series=(series,),
        statement=statement,
        source_event_ids=tuple(record.source_event_id for record in records),
        source_event_count=len(records),
        first_at=records[0].observed_at,
        last_at=records[-1].observed_at,
    )


def _normal(
    series: SeriesRef,
    predictions: Sequence[Prediction],
) -> VerifiedObservation:
    """State that a series was normal across the span, with no alert contribution."""
    ordered = sorted(predictions, key=lambda prediction: prediction.timestamp)
    statement = (
        f"{series.service} {series.metric_name.value} recorded {len(ordered)} normal "
        f"verdict(s) from {ordered[0].timestamp.isoformat()} to "
        f"{ordered[-1].timestamp.isoformat()}, and no measurement of it contributed "
        "to an alert in the span."
    )
    return VerifiedObservation(
        observation_id=observation_id(ObservationKind.NORMAL_SERIES, [series]),
        kind=ObservationKind.NORMAL_SERIES,
        series=(series,),
        statement=statement,
        source_event_ids=tuple(prediction.event_id for prediction in ordered),
        source_event_count=len(ordered),
        first_at=ordered[0].timestamp,
        last_at=ordered[-1].timestamp,
    )


def _onset(
    earlier: SeriesRef,
    earlier_record: EvidenceRecord,
    later: SeriesRef,
    later_record: EvidenceRecord,
) -> VerifiedObservation:
    """State which of two series first contributed to an alert, and by how much."""
    first, second = earlier_record.observed_at, later_record.observed_at
    if first < second:
        order = f"{earlier.metric_name.value} on {earlier.service} came first"
        gap = (second - first).total_seconds()
        timing = f"by {gap:g} seconds"
    elif second < first:
        order = f"{later.metric_name.value} on {later.service} came first"
        gap = (first - second).total_seconds()
        timing = f"by {gap:g} seconds"
    else:
        order = "both first contributed at the same tick"
        timing = ""
    statement = (
        f"{earlier.service} {earlier.metric_name.value} first contributed to an "
        f"alert at {first.isoformat()}; {later.service} {later.metric_name.value} "
        f"at {second.isoformat()}. {order}{' ' + timing if timing else ''}."
    )
    return VerifiedObservation(
        observation_id=observation_id(ObservationKind.ONSET_ORDER, [earlier, later]),
        kind=ObservationKind.ONSET_ORDER,
        series=(earlier, later),
        statement=statement,
        source_event_ids=(earlier_record.source_event_id, later_record.source_event_id),
        source_event_count=2,
        first_at=min(first, second),
        last_at=max(first, second),
    )
