"""Tests for metric-level and window-level evaluation.

Predictions are built by hand here, so every expected count can be checked by
reading the test. Detector behaviour is tested separately.
"""

from collections.abc import Collection

import pytest

from app.domain.telemetry import MetricName, TelemetryEvent, Unit
from app.services.analytics import SeriesKey, series_key
from app.services.detection import Decision, Prediction, Reason
from app.services.evaluation import (
    ConfusionMatrix,
    evaluate_effect_level,
    evaluate_metric_level,
    evaluate_window_level,
)
from app.services.incident import IncidentRule
from tests.telemetry_factories import TEST_RUN_ID, make_event

RUN_FAULT = TEST_RUN_ID
RUN_FAULT_SILENT = "sim-1111111111111111"
RUN_NORMAL_NOISY = "sim-2222222222222222"
RUN_NORMAL_QUIET = "sim-3333333333333333"
RUN_UNSCORED = "sim-4444444444444444"
RUN_UNSCORED_ONSET = "sim-5555555555555555"
RUN_UNSCORED_FAULT = "sim-6666666666666666"

LATENCY_KEY = SeriesKey(
    "orders-service",
    MetricName.REQUEST_LATENCY_MS,
    Unit.MILLISECONDS,
)


def _event(
    run_id: str,
    minute: int,
    *,
    anomalous: bool = False,
    service: str = "orders-service",
    sequence: int,
) -> TelemetryEvent:
    """Build a request-latency event for one run at one minute."""
    return make_event(
        service=service,
        metric=MetricName.REQUEST_LATENCY_MS,
        value=100.0,
        minute=minute,
        sequence=sequence,
        run_id=run_id,
        is_anomalous=anomalous,
    )


def _prediction(event: TelemetryEvent, decision: Decision) -> Prediction:
    """Return a prediction for ``event`` with the given decision."""
    reason = Reason.UNSEEN_SERIES if decision is Decision.UNSCORED else None
    return Prediction(
        event_id=event.event_id,
        timestamp=event.timestamp,
        key=series_key(event),
        decision=decision,
        score=None,
        reason=reason,
    )


def _predict(
    events: list[TelemetryEvent],
    alerts: Collection[tuple[str, int]],
    unscored: Collection[tuple[str, int]] = (),
) -> list[Prediction]:
    """Predict ANOMALOUS at (run, minute) pairs in ``alerts``, NORMAL elsewhere."""
    predictions: list[Prediction] = []
    for event in events:
        slot = (event.run_id, event.timestamp.minute)
        if slot in unscored:
            decision = Decision.UNSCORED
        elif slot in alerts:
            decision = Decision.ANOMALOUS
        else:
            decision = Decision.NORMAL
        predictions.append(_prediction(event, decision))
    return predictions


def _window_fixture() -> tuple[list[TelemetryEvent], list[Prediction]]:
    """Four scored runs and one unscored run, with expected outcomes in the tests.

    - RUN_FAULT: window at minutes 2-3. A pre-window alert at minute 0 and an
      in-window alert at minute 3. Detected, with delay 60 s from minute 2.
    - RUN_FAULT_SILENT: window at minutes 2-3. Only a pre-window alert, so it is
      not detected.
    - RUN_NORMAL_NOISY: no anomalous label, one alert. False positive.
    - RUN_NORMAL_QUIET: no anomalous label, no alerts. True negative.
    - RUN_UNSCORED: every event is unscored, so it must be excluded.
    """
    events: list[TelemetryEvent] = []
    sequence = 0

    def add(run_id: str, minutes: range, anomalous_minutes: range = range(0)) -> None:
        nonlocal sequence
        for minute in minutes:
            sequence += 1
            events.append(
                _event(
                    run_id,
                    minute,
                    anomalous=minute in anomalous_minutes,
                    sequence=sequence,
                )
            )

    add(RUN_FAULT, range(5), range(2, 4))
    add(RUN_FAULT_SILENT, range(5), range(2, 4))
    add(RUN_NORMAL_NOISY, range(3))
    add(RUN_NORMAL_QUIET, range(3))
    add(RUN_UNSCORED, range(3))
    predictions = _predict(
        events,
        alerts={
            (RUN_FAULT, 0),
            (RUN_FAULT, 3),
            (RUN_FAULT_SILENT, 0),
            (RUN_NORMAL_NOISY, 1),
        },
        unscored={(RUN_UNSCORED, minute) for minute in range(3)},
    )
    return events, predictions


# --- Confusion matrix and rates ---------------------------------------------


def test_from_pairs_counts_each_quadrant() -> None:
    pairs = (
        [(True, True)] * 3
        + [(False, True)]
        + [(True, False)] * 2
        + [(False, False)] * 4
    )

    matrix = ConfusionMatrix.from_pairs(pairs)

    assert (matrix.tp, matrix.fp, matrix.fn, matrix.tn) == (3, 1, 2, 4)
    assert matrix.total == 10


def test_rates_match_hand_computed_values() -> None:
    matrix = ConfusionMatrix(tp=3, fp=1, tn=4, fn=2)

    assert matrix.precision == pytest.approx(3 / 4)
    assert matrix.recall == pytest.approx(3 / 5)
    # F1 = 2 * 3 / (2 * 3 + 1 + 2) = 6 / 9, the harmonic mean of 0.75 and 0.6.
    assert matrix.f1 == pytest.approx(6 / 9)
    assert matrix.false_positive_rate == pytest.approx(1 / 5)


def test_f1_equals_harmonic_mean_of_precision_and_recall_when_defined() -> None:
    matrix = ConfusionMatrix(tp=7, fp=2, tn=10, fn=5)

    assert matrix.precision is not None and matrix.recall is not None
    harmonic = 2 * matrix.precision * matrix.recall / (matrix.precision + matrix.recall)
    assert matrix.f1 == pytest.approx(harmonic)


def test_no_flags_makes_precision_undefined_not_zero() -> None:
    matrix = ConfusionMatrix(tp=0, fp=0, tn=5, fn=2)

    assert matrix.precision is None
    assert matrix.recall == 0.0
    assert matrix.f1 == 0.0


def test_no_positives_makes_recall_and_f1_undefined() -> None:
    matrix = ConfusionMatrix(tp=0, fp=0, tn=5, fn=0)

    assert matrix.recall is None
    assert matrix.f1 is None
    assert matrix.false_positive_rate == 0.0


def test_no_normal_events_makes_false_positive_rate_undefined() -> None:
    matrix = ConfusionMatrix(tp=2, fp=0, tn=0, fn=1)

    assert matrix.false_positive_rate is None


def test_empty_matrix_is_undefined_everywhere() -> None:
    matrix = ConfusionMatrix(tp=0, fp=0, tn=0, fn=0)

    assert (matrix.precision, matrix.recall, matrix.f1, matrix.false_positive_rate) == (
        None,
        None,
        None,
        None,
    )


# --- Metric-level evaluation ------------------------------------------------


def test_metric_level_pools_events_and_breaks_down_by_series() -> None:
    events = [
        _event(TEST_RUN_ID, 0, sequence=1),
        _event(TEST_RUN_ID, 1, anomalous=True, sequence=2),
        _event(TEST_RUN_ID, 2, anomalous=True, sequence=3),
        _event(TEST_RUN_ID, 0, service="api-gateway", sequence=4),
    ]
    predictions = _predict(events, alerts={(TEST_RUN_ID, 0), (TEST_RUN_ID, 2)})

    report = evaluate_metric_level(events, predictions)

    # Alerts are keyed by run and minute, so the gateway event at minute 0 is also
    # flagged. Pooled: orders minute 0 and gateway minute 0 are FP, orders minute
    # 1 is FN, and orders minute 2 is TP.
    assert report.confusion == ConfusionMatrix(tp=1, fp=2, tn=0, fn=1)
    assert report.unscored_count == 0
    by_key = {item.key: item.confusion for item in report.by_series}
    assert by_key[LATENCY_KEY] == ConfusionMatrix(tp=1, fp=1, tn=0, fn=1)
    assert len(report.by_series) == 2


def test_metric_level_excludes_unscored_events_and_counts_them() -> None:
    events = [
        _event(TEST_RUN_ID, 0, sequence=1),
        _event(TEST_RUN_ID, 1, anomalous=True, sequence=2),
    ]
    predictions = _predict(events, alerts=set(), unscored={(TEST_RUN_ID, 0)})

    report = evaluate_metric_level(events, predictions)

    assert report.unscored_count == 1
    assert report.confusion == ConfusionMatrix(tp=0, fp=0, tn=0, fn=1)


def test_missing_prediction_is_rejected() -> None:
    events = [_event(TEST_RUN_ID, 0, sequence=1), _event(TEST_RUN_ID, 1, sequence=2)]
    predictions = _predict(events[:1], alerts=set())

    with pytest.raises(ValueError, match="cover exactly"):
        evaluate_metric_level(events, predictions)


def test_extra_prediction_is_rejected() -> None:
    events = [_event(TEST_RUN_ID, 0, sequence=1)]
    stray = _event(TEST_RUN_ID, 9, sequence=99)
    predictions = _predict([*events, stray], alerts=set())

    with pytest.raises(ValueError, match="cover exactly"):
        evaluate_metric_level(events, predictions)


def test_duplicate_prediction_is_rejected() -> None:
    events = [_event(TEST_RUN_ID, 0, sequence=1)]
    predictions = _predict(events, alerts=set()) * 2

    with pytest.raises(ValueError, match="only one prediction"):
        evaluate_metric_level(events, predictions)


# --- Window-level evaluation and detection delay ----------------------------


def test_window_level_confusion_over_runs() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)

    # One fault run detected, one missed, one normal run alerted, one quiet.
    assert report.confusion == ConfusionMatrix(tp=1, fp=1, tn=1, fn=1)


def test_detection_delay_is_first_in_window_alert_minus_window_start() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)
    fault = next(item for item in report.outcomes if item.run_id == RUN_FAULT)

    # The window starts at minute 2. The pre-window alert at minute 0 is ignored
    # for detection, so the first in-window alert is minute 3: delay 60 s.
    assert fault.delay_seconds == 60.0
    assert fault.predicted_anomalous is True


def test_undetected_window_has_no_delay_and_is_excluded_from_delay_summary() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)
    silent = next(item for item in report.outcomes if item.run_id == RUN_FAULT_SILENT)

    assert silent.actual_anomalous is True
    assert silent.predicted_anomalous is False
    assert silent.delay_seconds is None
    assert report.delay_summary.sample_count == 1
    assert report.delay_summary.mean == 60.0


def test_window_bounds_come_from_anomalous_labels() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)
    fault = next(item for item in report.outcomes if item.run_id == RUN_FAULT)

    assert fault.window_start == events[2].timestamp
    assert fault.window_end == events[3].timestamp


def test_unscored_onset_tick_still_anchors_the_window_start() -> None:
    events = [
        _event(
            RUN_UNSCORED_ONSET,
            minute,
            anomalous=minute in (2, 3, 4, 5),
            sequence=minute + 1,
        )
        for minute in range(6)
    ]
    predictions = _predict(
        events,
        alerts={(RUN_UNSCORED_ONSET, 4)},
        unscored={(RUN_UNSCORED_ONSET, 2)},
    )

    report = evaluate_window_level(events, predictions)
    outcome = report.outcomes[0]

    # The onset at minute 2 is unscored, but its label still marks when the fault
    # began. The alert at minute 4 is 120 s after onset, not 60 s after the first
    # scored anomalous tick.
    assert outcome.window_start == events[2].timestamp
    assert outcome.delay_seconds == 120.0


def test_unscored_fault_run_stays_an_incident_not_a_normal_run() -> None:
    events = [
        _event(
            RUN_UNSCORED_FAULT,
            minute,
            anomalous=minute in (2, 3),
            sequence=minute + 1,
        )
        for minute in range(6)
    ]
    predictions = _predict(
        events,
        alerts={(RUN_UNSCORED_FAULT, 0)},
        unscored={(RUN_UNSCORED_FAULT, 2), (RUN_UNSCORED_FAULT, 3)},
    )

    report = evaluate_window_level(events, predictions)

    # The fault is still a positive incident that was missed. Recording it as a
    # normal run would turn the pre-onset alert into a false positive instead.
    assert report.outcomes[0].actual_anomalous is True
    assert report.outcomes[0].predicted_anomalous is False
    assert report.confusion == ConfusionMatrix(tp=0, fp=0, tn=0, fn=1)


def test_pre_incident_alerts_are_counted_separately_and_never_detect() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)
    by_run = {item.run_id: item for item in report.outcomes}

    assert by_run[RUN_FAULT].pre_incident_alert_count == 1
    assert by_run[RUN_FAULT].predicted_anomalous is True
    assert by_run[RUN_FAULT_SILENT].pre_incident_alert_count == 1
    assert by_run[RUN_FAULT_SILENT].predicted_anomalous is False
    assert by_run[RUN_NORMAL_NOISY].pre_incident_alert_count == 0
    assert report.pre_incident_alert_run_count == 2


def test_normal_run_is_a_false_positive_only_when_alerted() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)
    noisy = next(item for item in report.outcomes if item.run_id == RUN_NORMAL_NOISY)
    quiet = next(item for item in report.outcomes if item.run_id == RUN_NORMAL_QUIET)

    assert noisy.predicted_anomalous is True
    assert noisy.delay_seconds is None
    assert quiet.predicted_anomalous is False


def test_run_with_only_unscored_events_is_excluded_not_normal() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(events, predictions)

    assert report.unscored_run_count == 1
    assert all(item.run_id != RUN_UNSCORED for item in report.outcomes)
    assert report.confusion.total == 4


def test_no_detected_windows_gives_empty_delay_summary() -> None:
    events, _ = _window_fixture()
    silent_predictions = _predict(
        events,
        alerts=set(),
        unscored={(RUN_UNSCORED, minute) for minute in range(3)},
    )

    report = evaluate_window_level(events, silent_predictions)

    assert report.delay_summary.sample_count == 0
    assert report.delay_summary.mean is None


def test_window_level_results_do_not_depend_on_input_order() -> None:
    events, predictions = _window_fixture()

    forward = evaluate_window_level(events, predictions)
    reversed_report = evaluate_window_level(
        list(reversed(events)),
        list(reversed(predictions)),
    )

    assert forward == reversed_report


def test_evaluation_is_deterministic() -> None:
    events, predictions = _window_fixture()

    assert evaluate_metric_level(events, predictions) == evaluate_metric_level(
        events, predictions
    )
    assert evaluate_window_level(events, predictions) == evaluate_window_level(
        events, predictions
    )


def test_persistence_rule_suppresses_isolated_run_level_false_positive() -> None:
    events, predictions = _window_fixture()

    report = evaluate_window_level(
        events,
        predictions,
        IncidentRule(min_consecutive_ticks=2),
    )
    outcomes = {outcome.run_id: outcome for outcome in report.outcomes}

    # The noisy normal run had one isolated alert, so it is now a true negative.
    assert outcomes[RUN_NORMAL_NOISY].predicted_anomalous is False
    # The fault run's two alerts are both isolated, so the rule now misses it.
    # Persistence trades false positives for missed detections.
    assert outcomes[RUN_FAULT].predicted_anomalous is False


def test_effect_level_counts_labelled_but_unaffected_series_as_normal() -> None:
    # DB spikes change request latency. They do not change HTTP error counts, even
    # though the window label marks every series anomalous.
    affected = make_event(
        service="orders-service",
        metric=MetricName.REQUEST_LATENCY_MS,
        value=900.0,
        minute=1,
        sequence=1,
        is_anomalous=True,
    )
    unaffected = make_event(
        service="orders-service",
        metric=MetricName.HTTP_ERROR_COUNT,
        value=0.0,
        minute=1,
        sequence=2,
        is_anomalous=True,
    )
    missed = make_event(
        service="orders-service",
        metric=MetricName.REQUEST_LATENCY_MS,
        value=100.0,
        minute=2,
        sequence=3,
        is_anomalous=True,
    )
    healthy = make_event(
        service="orders-service",
        metric=MetricName.REQUEST_LATENCY_MS,
        value=100.0,
        minute=3,
        sequence=4,
    )
    events = [affected, unaffected, missed, healthy]
    predictions = [
        _prediction(affected, Decision.ANOMALOUS),
        _prediction(unaffected, Decision.ANOMALOUS),
        _prediction(missed, Decision.NORMAL),
        _prediction(healthy, Decision.NORMAL),
    ]

    labelled = evaluate_metric_level(events, predictions).confusion
    effect = evaluate_effect_level(events, predictions)

    assert labelled == ConfusionMatrix(tp=2, fp=0, tn=1, fn=1)
    assert effect == ConfusionMatrix(tp=1, fp=1, tn=1, fn=1)


def test_effect_level_excludes_unscored_events() -> None:
    events, predictions = _window_fixture()
    unscored = [
        prediction
        for prediction in predictions
        if prediction.decision is Decision.UNSCORED
    ]

    effect = evaluate_effect_level(events, predictions)

    assert effect.total == len(events) - len(unscored)
