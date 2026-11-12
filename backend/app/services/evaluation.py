"""Scoring of detector predictions against ground-truth labels.

This module is the only place that reads ``label.is_anomalous``, and it does so
after predictions exist. It reports two levels that answer different questions:

- Metric level: every scored event is one decision. Ground truth is the event's
  scenario-window label. Results are pooled and also broken down per series.
- Window level: every evaluation run is one decision. A run whose events carry
  an anomalous label contains an incident window. A run is detected when an
  alert falls inside that window. Detection delay is measured here. Alerts come
  from an ``IncidentRule`` applied to the run's predictions.

Ground-truth labels mark whole fault windows, not individual metrics. A metric
that the fault does not change is still labelled anomalous during the window,
so metric-level false negatives include metrics the fault never affected.
``AFFECTED_SERIES`` records which series each fault changes, and the effect
level scores against that. It is a reporting annotation and never reaches a
detector.

Zero denominators. A ratio is reported as ``None`` when its denominator is zero,
never as 0 or 1, because an undefined rate must not look like a result:

- precision = TP / (TP + FP), undefined when no event was flagged;
- recall = TP / (TP + FN), undefined when no event is anomalous;
- F1 = 2TP / (2TP + FP + FN), undefined when there are no positives on either
  side. It equals the harmonic mean of precision and recall wherever they are
  both defined;
- false-positive rate = FP / (FP + TN), undefined when there are no normal
  events.

Unscored events are excluded from every confusion matrix and counted in
``unscored_count``. They are not counted as normal, because a detector that
abstains has not said the event is healthy.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.telemetry import MetricName, ScenarioId, TelemetryEvent
from app.services.analytics import SeriesKey, series_key
from app.services.detection import Decision, Prediction
from app.services.incident import ANY_POSITIVE_RULE, IncidentRule
from app.services.statistics import DescriptiveStats, describe

ORDERS_DB = "orders-db"
ORDERS_SERVICE = "orders-service"
API_GATEWAY = "api-gateway"

# (service, metric) pairs whose distribution the simulator changes inside the
# fault window. Mirrors the fault effects in ``simulation.py``. Kept here, not in
# the simulator, because it is scoring data: a detector never reads it.
AFFECTED_SERIES: dict[ScenarioId, frozenset[tuple[str, MetricName]]] = {
    ScenarioId.NORMAL_OPERATION: frozenset(),
    ScenarioId.DATABASE_LATENCY_SPIKE: frozenset(
        {
            (ORDERS_DB, MetricName.DB_QUERY_LATENCY_MS),
            (ORDERS_DB, MetricName.ACTIVE_DB_CONNECTIONS),
            (API_GATEWAY, MetricName.REQUEST_LATENCY_MS),
            (ORDERS_SERVICE, MetricName.REQUEST_LATENCY_MS),
        }
    ),
    ScenarioId.CPU_SATURATION: frozenset(
        {
            (ORDERS_SERVICE, MetricName.CPU_UTILIZATION_PERCENT),
            (API_GATEWAY, MetricName.REQUEST_LATENCY_MS),
            (ORDERS_SERVICE, MetricName.REQUEST_LATENCY_MS),
        }
    ),
    ScenarioId.API_ERRORS: frozenset(
        {
            (API_GATEWAY, MetricName.HTTP_ERROR_COUNT),
            (ORDERS_SERVICE, MetricName.HTTP_ERROR_COUNT),
            (API_GATEWAY, MetricName.REQUEST_LATENCY_MS),
            (ORDERS_SERVICE, MetricName.REQUEST_LATENCY_MS),
        }
    ),
    ScenarioId.CONNECTION_FAILURE: frozenset(
        {
            (ORDERS_SERVICE, MetricName.DB_CONNECTION_FAILURES),
            (ORDERS_DB, MetricName.ACTIVE_DB_CONNECTIONS),
            (API_GATEWAY, MetricName.HTTP_ERROR_COUNT),
            (ORDERS_SERVICE, MetricName.HTTP_ERROR_COUNT),
            (API_GATEWAY, MetricName.REQUEST_LATENCY_MS),
            (ORDERS_SERVICE, MetricName.REQUEST_LATENCY_MS),
        }
    ),
}


def _ratio(numerator: int, denominator: int) -> float | None:
    """Return numerator / denominator, or None when the ratio is undefined."""
    if denominator == 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class ConfusionMatrix:
    """Counts of true and false positives and negatives for one level.

    Rows are ground truth and columns are predictions:
    ``[[TN, FP], [FN, TP]]`` with ``TN`` the normal-and-not-flagged cell.
    """

    tp: int
    fp: int
    tn: int
    fn: int

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[bool, bool]]) -> "ConfusionMatrix":
        """Count (actual_anomalous, predicted_anomalous) pairs into a matrix."""
        tp = fp = tn = fn = 0
        for actual, predicted in pairs:
            if actual and predicted:
                tp += 1
            elif predicted:
                fp += 1
            elif actual:
                fn += 1
            else:
                tn += 1
        return cls(tp=tp, fp=fp, tn=tn, fn=fn)

    @property
    def total(self) -> int:
        """Number of decisions counted in this matrix."""
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self) -> float | None:
        """Share of flagged decisions that were anomalous."""
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        """Share of anomalous decisions that were flagged."""
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float | None:
        """Harmonic mean of precision and recall, from the counts directly."""
        return _ratio(2 * self.tp, 2 * self.tp + self.fp + self.fn)

    @property
    def false_positive_rate(self) -> float | None:
        """Share of normal decisions that were flagged."""
        return _ratio(self.fp, self.fp + self.tn)


@dataclass(frozen=True)
class SeriesConfusion:
    """Metric-level confusion for one (service, metric, unit) series."""

    key: SeriesKey
    confusion: ConfusionMatrix


@dataclass(frozen=True)
class MetricLevelReport:
    """Per-event evaluation, pooled over all series and broken down per series."""

    confusion: ConfusionMatrix
    unscored_count: int
    by_series: tuple[SeriesConfusion, ...]


@dataclass(frozen=True)
class WindowOutcome:
    """Window-level result for one evaluation run.

    ``window_start`` is the first anomalous timestamp and ``window_end`` the
    last, both inclusive. Both are None for a run with no anomalous events.
    ``delay_seconds`` is set only for a detected anomalous run.
    ``pre_incident_alert_count`` counts alerts raised before ``window_start``.
    They are never a detection. Normal runs carry 0 here, because their alerts
    are already false positives in the confusion matrix.
    """

    run_id: str
    actual_anomalous: bool
    predicted_anomalous: bool
    window_start: datetime | None
    window_end: datetime | None
    delay_seconds: float | None
    pre_incident_alert_count: int


@dataclass(frozen=True)
class WindowLevelReport:
    """Per-run evaluation of incident detection, with detection delay.

    ``delay_summary`` covers detected windows only. Undetected windows have no
    delay, and they are visible through the confusion matrix instead.
    ``pre_incident_alert_run_count`` counts anomalous runs that alerted before
    their onset. Those runs are reported here, not as detections.
    """

    confusion: ConfusionMatrix
    unscored_run_count: int
    outcomes: tuple[WindowOutcome, ...]
    delay_summary: DescriptiveStats
    pre_incident_alert_run_count: int


def _prediction_index(
    events: Sequence[TelemetryEvent],
    predictions: Iterable[Prediction],
) -> dict[str, Prediction]:
    """Map each evaluation event to its prediction, rejecting any mismatch.

    Every event needs exactly one prediction and no prediction may refer to an
    unknown event. A mismatch would silently drop or misalign results.
    """
    by_id: dict[str, Prediction] = {}
    for prediction in predictions:
        if prediction.event_id in by_id:
            raise ValueError("each event may have only one prediction")
        by_id[prediction.event_id] = prediction
    event_ids = {event.event_id for event in events}
    if set(by_id) != event_ids:
        raise ValueError("predictions must cover exactly the evaluated events")
    return by_id


def evaluate_metric_level(
    events: Sequence[TelemetryEvent],
    predictions: Iterable[Prediction],
) -> MetricLevelReport:
    """Score each event independently, pooled and per series."""
    by_id = _prediction_index(events, predictions)
    pooled: list[tuple[bool, bool]] = []
    per_series: dict[SeriesKey, list[tuple[bool, bool]]] = {}
    unscored = 0
    for event in sorted(events, key=lambda item: item.event_id):
        prediction = by_id[event.event_id]
        if prediction.decision is Decision.UNSCORED:
            unscored += 1
            continue
        pair = (event.label.is_anomalous, prediction.decision is Decision.ANOMALOUS)
        pooled.append(pair)
        per_series.setdefault(series_key(event), []).append(pair)
    return MetricLevelReport(
        confusion=ConfusionMatrix.from_pairs(pooled),
        unscored_count=unscored,
        by_series=tuple(
            SeriesConfusion(key=key, confusion=ConfusionMatrix.from_pairs(pairs))
            for key, pairs in sorted(per_series.items())
        ),
    )


def evaluate_effect_level(
    events: Sequence[TelemetryEvent],
    predictions: Iterable[Prediction],
) -> ConfusionMatrix:
    """Score each event against whether its series was genuinely affected.

    An event is anomalous only when it is inside a fault window and its series is
    in ``AFFECTED_SERIES`` for that fault. Labelled but unaffected series in the
    window count as normal, so this view separates detector misses from metrics
    the fault never changed. Pooled over all series; unscored events are excluded.
    """
    by_id = _prediction_index(events, predictions)
    pairs: list[tuple[bool, bool]] = []
    for event in sorted(events, key=lambda item: item.event_id):
        prediction = by_id[event.event_id]
        if prediction.decision is Decision.UNSCORED:
            continue
        affected = (
            event.label.is_anomalous
            and (event.service, event.metric_name)
            in AFFECTED_SERIES[event.label.scenario_id]
        )
        pairs.append((affected, prediction.decision is Decision.ANOMALOUS))
    return ConfusionMatrix.from_pairs(pairs)


def evaluate_window_level(
    events: Sequence[TelemetryEvent],
    predictions: Iterable[Prediction],
    rule: IncidentRule = ANY_POSITIVE_RULE,
) -> WindowLevelReport:
    """Score each evaluation run as one incident-detection decision.

    The run's scored predictions go through ``rule`` to give its alert times. A
    run is a predicted positive if the rule raises any alert. For an anomalous
    run, only alerts inside the labelled window count as detection. Alerts
    before the window are still false positives at metric level, but they do not
    change the window outcome. Detection delay is the first in-window alert
    minus the window start. A run with no scored events is excluded and counted
    in ``unscored_run_count``, never recorded as normal.

    The default rule alerts on any anomalous verdict, which is the original
    policy. The window bounds come from labels, but they only select which alerts
    count for scoring. They never affect when an alert fires.

    The window is taken from every labelled event in the run, scored or not.
    Labels are ground truth, so an unscored onset tick still marks when the fault
    began. Taking the window from scored events alone would understate the delay
    and could file a faulted run as normal.
    """
    by_id = _prediction_index(events, predictions)
    runs: dict[str, list[TelemetryEvent]] = {}
    for event in events:
        runs.setdefault(event.run_id, []).append(event)

    outcomes: list[WindowOutcome] = []
    unscored_runs = 0
    for run_id, run_events in sorted(runs.items()):
        scored = [
            (event, by_id[event.event_id])
            for event in sorted(run_events, key=lambda item: item.event_id)
            if by_id[event.event_id].decision is not Decision.UNSCORED
        ]
        if not scored:
            unscored_runs += 1
            continue
        anomalous_times = [
            event.timestamp for event in run_events if event.label.is_anomalous
        ]
        alerts = rule.alert_times(prediction for _, prediction in scored)
        if not anomalous_times:
            outcomes.append(
                WindowOutcome(
                    run_id=run_id,
                    actual_anomalous=False,
                    predicted_anomalous=bool(alerts),
                    window_start=None,
                    window_end=None,
                    delay_seconds=None,
                    pre_incident_alert_count=0,
                )
            )
            continue

        window_start = min(anomalous_times)
        window_end = max(anomalous_times)
        pre_incident = [alert for alert in alerts if alert < window_start]
        in_window = [alert for alert in alerts if window_start <= alert <= window_end]
        first_alert = min(in_window, default=None)
        outcomes.append(
            WindowOutcome(
                run_id=run_id,
                actual_anomalous=True,
                predicted_anomalous=first_alert is not None,
                window_start=window_start,
                window_end=window_end,
                delay_seconds=(
                    None
                    if first_alert is None
                    else (first_alert - window_start).total_seconds()
                ),
                pre_incident_alert_count=len(pre_incident),
            )
        )

    confusion = ConfusionMatrix.from_pairs(
        (outcome.actual_anomalous, outcome.predicted_anomalous) for outcome in outcomes
    )
    delays = [outcome.delay_seconds for outcome in outcomes]
    return WindowLevelReport(
        confusion=confusion,
        unscored_run_count=unscored_runs,
        outcomes=tuple(outcomes),
        delay_summary=describe(delays),
        pre_incident_alert_run_count=sum(
            1 for outcome in outcomes if outcome.pre_incident_alert_count > 0
        ),
    )
