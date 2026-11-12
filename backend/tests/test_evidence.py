"""Tests for evidence collection and telemetry gap detection.

Evidence must point at stored events and copy their values. A gap is any pair
of consecutive observations further apart than one tick, whatever order the
events arrived in.
"""

from datetime import timedelta

from app.domain.incident_records import InclusionReason
from app.domain.telemetry import MetricName, Unit
from app.services.analytics import SeriesKey
from app.services.correlation import anomalies_by_tick
from app.services.detection import Decision
from app.services.detectors import Direction, ZScoreConfig, ZScoreDetector
from app.services.evidence import collect_evidence, find_telemetry_gaps
from tests.telemetry_factories import TEST_START, make_event

BASE_RUN = "sim-aaaaaaaaaaaaaaaa"
RUN = "sim-bbbbbbbbbbbbbbbb"
LATENCY = SeriesKey(
    "orders-service",
    MetricName.REQUEST_LATENCY_MS,
    Unit.MILLISECONDS,
)


def _events_at(minutes: list[int], run_id: str = RUN) -> list:
    """Build one event per minute offset, with sequence numbers in input order."""
    return [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=100.0,
            minute=minute,
            sequence=index + 1,
            run_id=run_id,
        )
        for index, minute in enumerate(minutes)
    ]


def test_gap_is_reported_between_the_observations_either_side_of_it() -> None:
    events = _events_at([0, 1, 2, 5, 6])

    gaps = find_telemetry_gaps(events, tick_interval_seconds=60)

    assert len(gaps) == 1
    assert gaps[0].observed_before == TEST_START + timedelta(minutes=2)
    assert gaps[0].observed_after == TEST_START + timedelta(minutes=5)


def test_consecutive_ticks_have_no_gap() -> None:
    assert find_telemetry_gaps(_events_at([0, 1, 2, 3]), 60) == []


def test_gap_detection_ignores_arrival_order_and_duplicate_timestamps() -> None:
    in_order = _events_at([0, 1, 5, 6])
    shuffled = list(reversed(in_order)) + _events_at([1], run_id=RUN)

    assert find_telemetry_gaps(shuffled, 60) == find_telemetry_gaps(in_order, 60)


def test_no_events_means_no_gaps() -> None:
    assert find_telemetry_gaps([], 60) == []


def test_evidence_copies_value_and_timestamp_from_the_stored_event() -> None:
    training = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=100.0 + (minute % 3),
            minute=minute,
            sequence=minute + 1,
            run_id=BASE_RUN,
        )
        for minute in range(12)
    ]
    detector = ZScoreDetector(ZScoreConfig(direction=Direction.UPPER_TAIL))
    detector.fit(training)
    run_events = _events_at([0], run_id=RUN)
    run_events[0] = run_events[0].model_copy(update={"value": 400.0})
    predictions = detector.predict(run_events)
    assert predictions[0].decision is Decision.ANOMALOUS

    evidence = collect_evidence(
        series=frozenset({LATENCY}),
        alert_ticks=frozenset({TEST_START}),
        span_start=TEST_START,
        span_end=TEST_START,
        first_detection_at=TEST_START,
        anomalies=anomalies_by_tick(predictions),
        events_by_id={event.event_id: event for event in run_events},
        detector=detector,
        training_run_ids=[BASE_RUN],
    )

    record = evidence[0]
    assert record.source_event_id == run_events[0].event_id
    assert record.observed_value == 400.0
    assert record.observed_at == TEST_START
    assert record.inclusion_reason is InclusionReason.CONTRIBUTED_TO_ALERT
    assert record.baseline_comparison is not None
    assert record.baseline_comparison.training_run_ids == (BASE_RUN,)
    assert record.baseline_comparison.score == predictions[0].score


def test_zero_spread_anomaly_has_evidence_but_no_invented_baseline_score() -> None:
    # A constant baseline has no spread, so any deviation is flagged with no
    # score. Evidence must still be recorded, with no baseline comparison.
    training = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=100.0,
            minute=minute,
            sequence=minute + 1,
            run_id=BASE_RUN,
        )
        for minute in range(12)
    ]
    detector = ZScoreDetector(ZScoreConfig(direction=Direction.UPPER_TAIL))
    detector.fit(training)
    run_events = _events_at([0], run_id=RUN)
    run_events[0] = run_events[0].model_copy(update={"value": 250.0})
    predictions = detector.predict(run_events)

    evidence = collect_evidence(
        series=frozenset({LATENCY}),
        alert_ticks=frozenset(),
        span_start=TEST_START,
        span_end=TEST_START,
        first_detection_at=TEST_START,
        anomalies=anomalies_by_tick(predictions),
        events_by_id={event.event_id: event for event in run_events},
        detector=detector,
        training_run_ids=[BASE_RUN],
    )

    assert len(evidence) == 1
    assert evidence[0].inclusion_reason is InclusionReason.ANOMALOUS_IN_WINDOW
    assert evidence[0].baseline_comparison is None


def test_evidence_outside_the_span_is_excluded() -> None:
    training = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=100.0 + (minute % 3),
            minute=minute,
            sequence=minute + 1,
            run_id=BASE_RUN,
        )
        for minute in range(12)
    ]
    detector = ZScoreDetector(ZScoreConfig(direction=Direction.UPPER_TAIL))
    detector.fit(training)
    run_events = _events_at([0, 5], run_id=RUN)
    run_events = [
        event.model_copy(update={"value": 400.0}) for event in run_events
    ]
    predictions = detector.predict(run_events)

    evidence = collect_evidence(
        series=frozenset({LATENCY}),
        alert_ticks=frozenset(),
        span_start=TEST_START,
        span_end=TEST_START + timedelta(minutes=1),
        first_detection_at=TEST_START,
        anomalies=anomalies_by_tick(predictions),
        events_by_id={event.event_id: event for event in run_events},
        detector=detector,
        training_run_ids=[BASE_RUN],
    )

    assert [record.observed_at for record in evidence] == [TEST_START]


def test_anomalies_are_classified_as_context_detection_or_measurement() -> None:
    training = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=100.0 + (minute % 3),
            minute=minute,
            sequence=minute + 1,
            run_id=BASE_RUN,
        )
        for minute in range(12)
    ]
    detector = ZScoreDetector(ZScoreConfig(direction=Direction.UPPER_TAIL))
    detector.fit(training)
    run_events = [
        event.model_copy(update={"value": 400.0})
        for event in _events_at([0, 5, 8], run_id=RUN)
    ]
    predictions = detector.predict(run_events)

    evidence = collect_evidence(
        series=frozenset({LATENCY}),
        alert_ticks=frozenset({TEST_START + timedelta(minutes=5)}),
        span_start=TEST_START,
        span_end=TEST_START + timedelta(minutes=8),
        first_detection_at=TEST_START + timedelta(minutes=5),
        anomalies=anomalies_by_tick(predictions),
        events_by_id={event.event_id: event for event in run_events},
        detector=detector,
        training_run_ids=[BASE_RUN],
    )

    reasons = [(record.observed_at, record.inclusion_reason) for record in evidence]
    assert reasons == [
        (TEST_START, InclusionReason.PRE_INCIDENT_CONTEXT),
        (TEST_START + timedelta(minutes=5), InclusionReason.CONTRIBUTED_TO_ALERT),
        (TEST_START + timedelta(minutes=8), InclusionReason.ANOMALOUS_IN_WINDOW),
    ]
