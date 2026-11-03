"""Tests for per-series summaries and single-series rolling analytics."""

import pytest

from app.domain.telemetry import MetricName, ScenarioId, Unit
from app.services.analytics import (
    SeriesKey,
    metric_unit,
    rolling_series,
    summarize,
)
from app.services.simulation import SimulationSpec, run_simulation
from tests.telemetry_factories import make_event


def test_summarize_keeps_services_and_metrics_apart() -> None:
    # Hand-calculated: api-gateway mean (100 + 200 + 300) / 3 = 200 and
    # orders-db mean (5 + 10) / 2 = 7.5. Combining them would give a
    # meaningless mean of 137.5 for "latency".
    events = [
        make_event(
            service="api-gateway",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=value,
            minute=index,
            sequence=index,
        )
        for index, value in enumerate([100.0, 200.0, 300.0])
    ] + [
        make_event(
            service="orders-db",
            metric=MetricName.DB_QUERY_LATENCY_MS,
            value=value,
            minute=index,
            sequence=10 + index,
        )
        for index, value in enumerate([5.0, 10.0])
    ]

    summaries = {summary.key: summary.stats for summary in summarize(events)}

    api = summaries[
        SeriesKey("api-gateway", MetricName.REQUEST_LATENCY_MS, Unit.MILLISECONDS)
    ]
    db = summaries[
        SeriesKey("orders-db", MetricName.DB_QUERY_LATENCY_MS, Unit.MILLISECONDS)
    ]
    assert api.sample_count == 3
    assert api.mean == pytest.approx(200.0)
    assert db.sample_count == 2
    assert db.mean == pytest.approx(7.5)


def test_same_metric_on_different_services_is_separate_series() -> None:
    events = [
        make_event(
            service="orders-service",
            metric=MetricName.CPU_UTILIZATION_PERCENT,
            value=40.0,
            minute=0,
            sequence=1,
        ),
        make_event(
            service="orders-db",
            metric=MetricName.CPU_UTILIZATION_PERCENT,
            value=90.0,
            minute=0,
            sequence=2,
        ),
    ]

    summaries = summarize(events)

    assert len(summaries) == 2
    assert {summary.key.service for summary in summaries} == {
        "orders-service",
        "orders-db",
    }


def test_full_simulation_yields_one_series_per_service_metric_pair() -> None:
    ticks = 3
    run = run_simulation(
        SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=5, ticks=ticks)
    )

    summaries = summarize(run.events)

    # The topology reports nine service and metric pairs, each once per tick.
    assert len(summaries) == 9
    assert all(summary.stats.sample_count == ticks for summary in summaries)


def test_summarize_empty_input_returns_no_series() -> None:
    assert summarize([]) == []


def test_metric_unit_comes_from_the_catalog() -> None:
    assert metric_unit(MetricName.CPU_UTILIZATION_PERCENT) is Unit.PERCENT
    assert metric_unit(MetricName.HTTP_ERROR_COUNT) is Unit.COUNT


def test_rolling_series_is_chronological_even_for_shuffled_input() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    events = [
        make_event(
            service="orders-db",
            metric=MetricName.DB_QUERY_LATENCY_MS,
            value=value,
            minute=minute,
            sequence=minute,
        )
        for minute, value in enumerate(values)
    ]

    points = rolling_series(list(reversed(events)), window=2)

    assert [point.value for point in points] == values
    assert [point.timestamp for point in points] == sorted(
        point.timestamp for point in points
    )
    # Window of two: mean of (10, 20) is 15, and of (30, 40) is 35.
    assert points[1].mean == pytest.approx(15.0)
    assert points[3].mean == pytest.approx(35.0)


def test_rolling_series_refuses_to_mix_services() -> None:
    events = [
        make_event(
            service="orders-db",
            metric=MetricName.DB_QUERY_LATENCY_MS,
            value=8.0,
            minute=0,
            sequence=1,
        ),
        make_event(
            service="api-gateway",
            metric=MetricName.REQUEST_LATENCY_MS,
            value=120.0,
            minute=1,
            sequence=2,
        ),
    ]

    with pytest.raises(ValueError, match="exactly one service"):
        rolling_series(events, window=2)


def test_rolling_series_refuses_to_mix_runs() -> None:
    other_run = "sim-fedcba9876543210"
    events = [
        make_event(
            service="orders-db",
            metric=MetricName.DB_QUERY_LATENCY_MS,
            value=8.0,
            minute=0,
            sequence=1,
        ),
        make_event(
            service="orders-db",
            metric=MetricName.DB_QUERY_LATENCY_MS,
            value=9.0,
            minute=0,
            sequence=2,
            run_id=other_run,
        ),
    ]

    with pytest.raises(ValueError, match="exactly one run"):
        rolling_series(events, window=2)


def test_rolling_series_of_nothing_is_empty() -> None:
    assert rolling_series([], window=3) == []


def test_rolling_series_marks_incomplete_windows() -> None:
    events = [
        make_event(
            service="orders-db",
            metric=MetricName.DB_QUERY_LATENCY_MS,
            value=value,
            minute=minute,
            sequence=minute,
        )
        for minute, value in enumerate([8.0, 8.0, 8.0])
    ]

    points = rolling_series(events, window=3)

    assert [point.window_count for point in points] == [1, 2, 3]
    assert points[0].mean is None
    assert points[2].stdev == 0.0
