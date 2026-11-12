"""Tests for the incident decision layer.

Verdicts are written as one string per tick, with one character per series: ``A``
anomalous, ``N`` normal, ``U`` unscored. That keeps every expected alert readable
in the test. Detector and experiment behaviour is tested in their own modules.
"""

from datetime import datetime, timedelta

import pytest

from app.domain.telemetry import GroundTruthLabel, MetricName, ScenarioId, Unit
from app.services.analytics import SeriesKey
from app.services.detection import Decision, Prediction, Reason
from app.services.detectors import ZScoreConfig, ZScoreDetector
from app.services.incident import ANY_POSITIVE_RULE, IncidentRule
from app.services.simulation import DEFAULT_TICK_INTERVAL_SECONDS
from tests.telemetry_factories import TEST_START, make_event

SERIES = (
    SeriesKey("orders-service", MetricName.REQUEST_LATENCY_MS, Unit.MILLISECONDS),
    SeriesKey("orders-service", MetricName.HTTP_ERROR_COUNT, Unit.COUNT),
    SeriesKey("orders-db", MetricName.DB_QUERY_LATENCY_MS, Unit.MILLISECONDS),
)
CODES = {"A": Decision.ANOMALOUS, "N": Decision.NORMAL, "U": Decision.UNSCORED}


def _predictions(*ticks: str) -> list[Prediction]:
    """Build one run's predictions, one verdict string per one-minute tick."""
    predictions: list[Prediction] = []
    for minute, verdicts in enumerate(ticks):
        timestamp = TEST_START + timedelta(minutes=minute)
        for index, (key, code) in enumerate(zip(SERIES, verdicts, strict=True)):
            predictions.append(
                Prediction(
                    event_id=f"p-{minute:03d}-{index}",
                    timestamp=timestamp,
                    key=key,
                    decision=CODES[code],
                    score=None,
                    reason=None,
                )
            )
    return predictions


def _minutes(alerts: list[datetime]) -> list[int]:
    """Return alert times as whole minutes after the test start."""
    return [int((alert - TEST_START).total_seconds() // 60) for alert in alerts]


def _constant_detector() -> ZScoreDetector:
    """A z-score detector fitted on a series that is exactly 100 in training."""
    training = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=100.0,
            minute=minute,
            sequence=minute + 1,
            run_id="sim-aaaaaaaaaaaaaaaa",
        )
        for minute in range(10)
    ]
    detector = ZScoreDetector(ZScoreConfig())
    detector.fit(training)
    return detector


def test_default_rule_alerts_on_every_tick_with_an_anomalous_verdict() -> None:
    predictions = _predictions("ANN", "NNN", "NAN")

    assert _minutes(ANY_POSITIVE_RULE.alert_times(predictions)) == [0, 2]


@pytest.mark.parametrize(
    "rule",
    [
        IncidentRule(),
        IncidentRule(min_affected_series=2, min_consecutive_ticks=3),
        IncidentRule(min_affected_series=3, min_consecutive_ticks=5),
    ],
)
def test_completely_normal_run_raises_no_alert(rule: IncidentRule) -> None:
    predictions = _predictions(*(["NNN"] * 60))

    assert rule.alert_times(predictions) == []


def test_isolated_false_positive_alerts_only_without_persistence() -> None:
    # One anomalous tick is the shape of a Poisson count spike in healthy data.
    predictions = _predictions("NNN", "ANN", "NNN")

    assert _minutes(IncidentRule(min_consecutive_ticks=1).alert_times(predictions)) == [
        1
    ]
    assert IncidentRule(min_consecutive_ticks=2).alert_times(predictions) == []


def test_consecutive_anomalous_ticks_alert_at_the_threshold_and_continue() -> None:
    predictions = _predictions("ANN", "ANN", "ANN", "ANN", "NNN")

    alerts = IncidentRule(min_consecutive_ticks=3).alert_times(predictions)

    assert _minutes(alerts) == [2, 3]


def test_a_normal_tick_resets_the_streak() -> None:
    predictions = _predictions("ANN", "ANN", "NNN", "ANN", "ANN")

    assert IncidentRule(min_consecutive_ticks=3).alert_times(predictions) == []


def test_repeated_verdicts_from_one_series_count_once_per_tick() -> None:
    # Two anomalous verdicts for the same series in one tick are still one series.
    first, *_ = _predictions("ANN")
    duplicate = Prediction(
        event_id="p-000-duplicate",
        timestamp=first.timestamp,
        key=first.key,
        decision=Decision.ANOMALOUS,
        score=None,
        reason=None,
    )

    rule = IncidentRule(min_affected_series=2)

    assert rule.alert_times([first, duplicate]) == []


def test_multiple_affected_series_must_agree_within_one_tick() -> None:
    spread_out = _predictions("ANN", "NAN")
    together = _predictions("AAN")
    rule = IncidentRule(min_affected_series=2)

    assert rule.alert_times(spread_out) == []
    assert _minutes(rule.alert_times(together)) == [0]


def test_fault_onset_alerts_after_persistence_and_stops_on_recovery() -> None:
    # Three healthy ticks, a fault that moves two series for four ticks, then
    # recovery. The rule needs two consecutive ticks with two affected series.
    predictions = _predictions(
        "NNN",
        "NNN",
        "NNN",
        "AAN",
        "AAN",
        "AAN",
        "AAN",
        "NNN",
        "NNN",
    )
    rule = IncidentRule(min_affected_series=2, min_consecutive_ticks=2)

    assert _minutes(rule.alert_times(predictions)) == [4, 5, 6]


def test_unscored_tick_breaks_a_streak() -> None:
    # An unscored tick has no evidence that the series is hot, so it is not hot.
    predictions = _predictions("ANN", "UUU", "ANN")

    alerts = IncidentRule(min_consecutive_ticks=2).alert_times(predictions)

    assert _minutes(alerts) == []


def _without_minutes(
    predictions: list[Prediction],
    *minutes: int,
) -> list[Prediction]:
    """Drop every prediction at the given minutes, as if that tick was never sent."""
    dropped = {TEST_START + timedelta(minutes=minute) for minute in minutes}
    return [p for p in predictions if p.timestamp not in dropped]


def test_missing_tick_breaks_the_streak() -> None:
    # Minute 2 is absent. Without the gap, minutes 2-5 would alert at 2, 3, 4 and
    # 5. With it, minutes 0-1 reach only two ticks, and the first three contiguous
    # hot ticks are 3-5, so the only alert is at minute 5.
    predictions = _without_minutes(_predictions(*["AAA"] * 6), 2)

    alerts = IncidentRule(min_consecutive_ticks=3).alert_times(predictions)

    assert _minutes(alerts) == [5]


def test_streak_restarts_after_a_gap_and_recovers() -> None:
    # Minutes 0-1 form one streak. Minute 2 is missing. Minutes 3-4 form a new
    # streak that reaches two ticks, so the alert comes at minute 4, not earlier.
    predictions = _without_minutes(_predictions(*["AAA"] * 6), 2)

    alerts = IncidentRule(min_consecutive_ticks=2).alert_times(predictions)

    assert _minutes(alerts) == [1, 4, 5]


def test_gap_is_measured_in_the_documented_tick_interval() -> None:
    # Minutes are 60 s apart. With a 120 s interval every tick is a gap, so no
    # streak of two can form, even though the records are consecutive.
    rule = IncidentRule(min_consecutive_ticks=2, tick_interval_seconds=120)

    assert _minutes(rule.alert_times(_predictions(*["AAA"] * 4))) == []


def test_default_interval_is_the_simulator_interval() -> None:
    assert IncidentRule().tick_interval_seconds == DEFAULT_TICK_INTERVAL_SECONDS


def test_alerts_across_gaps_use_only_the_current_and_earlier_ticks() -> None:
    # The causality check from above, repeated with a missing tick. Truncating
    # the run must still give exactly the earlier alerts of the full run.
    ticks = ["AAN", "AAA", "AAA", "AAA", "AAA"]
    rule = IncidentRule(min_consecutive_ticks=2)
    full = _predictions(*ticks)
    gapped = _without_minutes(full, 2)
    expected = _minutes(rule.alert_times(gapped))

    for cut in range(1, len(ticks) + 1):
        cutoff = TEST_START + timedelta(minutes=cut)
        prefix = [p for p in gapped if p.timestamp < cutoff]
        assert _minutes(rule.alert_times(prefix)) == [
            minute for minute in expected if minute < cut
        ]


def test_alerts_use_only_the_current_and_earlier_ticks() -> None:
    # Truncating the run must give exactly the alerts of the full run that fall at
    # or before the cut. A later tick that could change an earlier alert would
    # break this.
    ticks = ["ANN", "ANN", "NNN", "ANN", "ANN", "ANN", "NNN", "ANN"]
    rule = IncidentRule(min_consecutive_ticks=2)
    full = _minutes(rule.alert_times(_predictions(*ticks)))

    for cut in range(1, len(ticks) + 1):
        prefix = _minutes(rule.alert_times(_predictions(*ticks[:cut])))
        assert prefix == [minute for minute in full if minute < cut]


def test_zero_spread_deviation_reaches_the_rule_as_an_anomalous_verdict() -> None:
    # A constant healthy series has no spread, so any change is anomalous with no
    # score. It must count as an anomalous verdict, not be dropped.
    detector = _constant_detector()
    evaluation = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=value,
            minute=minute,
            sequence=100 + minute,
        )
        for minute, value in ((20, 100.0), (21, 150.0), (22, 150.0))
    ]

    predictions = detector.predict(evaluation)

    assert [p.reason for p in predictions] == [
        None,
        Reason.ZERO_SPREAD_DEVIATION,
        Reason.ZERO_SPREAD_DEVIATION,
    ]
    alerts = IncidentRule(min_consecutive_ticks=2).alert_times(predictions)
    assert alerts == [TEST_START + timedelta(minutes=22)]


def test_flipping_ground_truth_does_not_change_predictions_or_alerts() -> None:
    # Labels are never a detector input, and the rule sees predictions only.
    # Two copies of one run differ only in their labels.
    labelled = [
        make_event(
            service="orders-service",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=value,
            minute=minute,
            sequence=100 + minute,
            is_anomalous=is_anomalous,
        )
        for minute, (value, is_anomalous) in enumerate(
            [(150.0, True), (150.0, True), (100.0, False)]
        )
    ]
    flipped = [
        event.model_copy(
            update={
                "label": GroundTruthLabel(
                    scenario_id=ScenarioId.NORMAL_OPERATION,
                    is_anomalous=False,
                )
            }
        )
        for event in labelled
    ]
    detector = _constant_detector()
    rule = IncidentRule(min_consecutive_ticks=2)

    assert detector.predict(labelled) == detector.predict(flipped)
    assert rule.alert_times(detector.predict(labelled)) == rule.alert_times(
        detector.predict(flipped)
    )


def test_invalid_requirements_are_rejected() -> None:
    with pytest.raises(ValueError, match="min_affected_series"):
        IncidentRule(min_affected_series=0)
    with pytest.raises(ValueError, match="min_consecutive_ticks"):
        IncidentRule(min_consecutive_ticks=0)
    with pytest.raises(ValueError, match="tick_interval_seconds"):
        IncidentRule(tick_interval_seconds=0)
