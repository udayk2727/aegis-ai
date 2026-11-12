"""Tests for alert grouping: time windows, topology, duplicates, and ordering.

Alerts are built by hand, so each expected grouping can be read straight off the
test. Each alert is written as a minute offset from the test start.
"""

import random
from datetime import timedelta

import pytest

from app.domain.incident_records import CorrelationKind
from app.domain.telemetry import MetricName, Unit
from app.services.analytics import SeriesKey
from app.services.correlation import (
    Alert,
    CorrelationConfig,
    anomalies_by_tick,
    build_alerts,
    correlation_rationale,
    group_alerts,
)
from app.services.detection import Decision, Prediction, Reason
from app.services.topology import DEFAULT_TOPOLOGY
from tests.telemetry_factories import TEST_START

GATEWAY = SeriesKey("api-gateway", MetricName.REQUEST_LATENCY_MS, Unit.MILLISECONDS)
ORDERS = SeriesKey("orders-service", MetricName.CPU_UTILIZATION_PERCENT, Unit.PERCENT)
DATABASE = SeriesKey("orders-db", MetricName.DB_QUERY_LATENCY_MS, Unit.MILLISECONDS)
WINDOW_SECONDS = 600


def alert(minute: int, *keys: SeriesKey) -> Alert:
    """Build an alert at ``minute`` after the test start with the given series."""
    return Alert(
        timestamp=TEST_START + timedelta(minutes=minute),
        keys=tuple(sorted(keys)),
    )


def minutes(groups: list[list[Alert]]) -> list[list[int]]:
    """Return each group as its alert minutes, for readable assertions."""
    return [
        [int((item.timestamp - TEST_START).total_seconds() // 60) for item in group]
        for group in groups
    ]


def group(alerts: list[Alert], window: int = WINDOW_SECONDS) -> list[list[int]]:
    """Group alerts with the default topology and return their minutes."""
    return minutes(group_alerts(alerts, DEFAULT_TOPOLOGY, window))


def test_related_alerts_within_the_window_are_one_incident() -> None:
    alerts = [alert(0, ORDERS), alert(4, ORDERS), alert(8, GATEWAY)]

    assert group(alerts) == [[0, 4, 8]]


def test_gap_of_exactly_the_window_links_the_alerts() -> None:
    alerts = [alert(0, ORDERS), alert(10, ORDERS)]

    assert group(alerts, window=600) == [[0, 10]]


def test_gap_one_second_beyond_the_window_separates_the_alerts() -> None:
    first = alert(0, ORDERS)
    second = Alert(
        timestamp=first.timestamp + timedelta(seconds=601),
        keys=(ORDERS,),
    )

    assert minutes(group_alerts([first, second], DEFAULT_TOPOLOGY, 600)) == [
        [0],
        [10],
    ]


def test_directly_connected_services_correlate_across_the_window() -> None:
    alerts = [alert(0, GATEWAY), alert(3, ORDERS)]

    assert group(alerts) == [[0, 3]]


def test_services_with_no_declared_dependency_stay_separate() -> None:
    # api-gateway calls orders-service, but nothing calls orders-db directly
    # from api-gateway, so the two alerts are unrelated despite being close.
    alerts = [alert(0, GATEWAY), alert(1, DATABASE)]

    assert group(alerts) == [[0], [1]]


def test_a_chain_of_related_alerts_can_span_longer_than_one_window() -> None:
    # gateway at 0 and db at 10 are 10 minutes apart, which is the window.
    # Each is linked to orders at 5, so all three join through the chain.
    alerts = [alert(0, GATEWAY), alert(5, ORDERS), alert(10, DATABASE)]

    assert group(alerts, window=300) == [[0, 5, 10]]


def test_independent_incidents_far_apart_in_time_are_not_merged() -> None:
    alerts = [alert(0, ORDERS), alert(60, ORDERS)]

    assert group(alerts) == [[0], [60]]


def test_out_of_order_input_gives_the_same_groups() -> None:
    alerts = [
        alert(0, GATEWAY),
        alert(2, ORDERS),
        alert(30, DATABASE),
        alert(31, DATABASE),
        alert(4, GATEWAY),
    ]
    expected = group(alerts)
    for seed in range(5):
        shuffled = list(alerts)
        random.Random(seed).shuffle(shuffled)
        assert group(shuffled) == expected
    assert expected == [[0, 2, 4], [30, 31]]


def test_groups_are_ordered_by_their_first_alert() -> None:
    alerts = [alert(90, DATABASE), alert(0, ORDERS), alert(45, GATEWAY)]

    assert group(alerts) == [[0], [45], [90]]


def test_no_alerts_gives_no_groups() -> None:
    assert group([]) == []


def test_anomalies_by_tick_collapses_repeated_predictions() -> None:
    timestamp = TEST_START
    anomalous = Prediction(
        event_id="evt-1",
        timestamp=timestamp,
        key=ORDERS,
        decision=Decision.ANOMALOUS,
        score=4.0,
        reason=None,
    )
    duplicate = Prediction(
        event_id="evt-1",
        timestamp=timestamp,
        key=ORDERS,
        decision=Decision.ANOMALOUS,
        score=4.0,
        reason=None,
    )
    unscored = Prediction(
        event_id="evt-2",
        timestamp=timestamp,
        key=GATEWAY,
        decision=Decision.UNSCORED,
        score=None,
        reason=Reason.UNSEEN_SERIES,
    )

    index = anomalies_by_tick([anomalous, duplicate, unscored])

    assert list(index) == [timestamp]
    assert list(index[timestamp]) == [ORDERS]


def test_build_alerts_counts_a_tick_once_however_often_it_is_listed() -> None:
    tick = TEST_START
    prediction = Prediction(
        event_id="evt-1",
        timestamp=tick,
        key=ORDERS,
        decision=Decision.ANOMALOUS,
        score=4.0,
        reason=None,
    )
    index = anomalies_by_tick([prediction])

    alerts = build_alerts([tick, tick], index)

    assert len(alerts) == 1
    assert alerts[0].keys == (ORDERS,)


def test_rationale_always_states_that_correlation_is_not_causal() -> None:
    alerts = [alert(0, GATEWAY), alert(3, ORDERS)]

    reasons = correlation_rationale(alerts, DEFAULT_TOPOLOGY, WINDOW_SECONDS)

    kinds = [reason.kind for reason in reasons]
    assert CorrelationKind.NOT_CAUSAL in kinds
    assert CorrelationKind.TEMPORAL_PROXIMITY in kinds


def test_rationale_names_the_declared_dependency_used() -> None:
    alerts = [alert(0, ORDERS), alert(2, DATABASE)]

    reasons = correlation_rationale(alerts, DEFAULT_TOPOLOGY, WINDOW_SECONDS)

    dependencies = [
        reason.detail
        for reason in reasons
        if reason.kind is CorrelationKind.DEPENDENCY_LINK
    ]
    assert dependencies == ["orders-service calls orders-db in the declared topology"]


def test_single_alert_rationale_says_nothing_else_was_linked() -> None:
    reasons = correlation_rationale(
        [alert(0, ORDERS)],
        DEFAULT_TOPOLOGY,
        WINDOW_SECONDS,
    )

    assert reasons[0].kind is CorrelationKind.SINGLE_ALERT


def test_window_must_be_at_least_one_second() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        CorrelationConfig(window_seconds=0)
