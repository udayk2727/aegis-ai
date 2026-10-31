"""Tests for the simulation service: scenarios, labels, determinism, and bounds."""

import math
from datetime import UTC, datetime, timedelta, timezone
from statistics import mean

import pytest
from pydantic import ValidationError

from app.domain.telemetry import (
    METRIC_SPECS,
    MetricName,
    ScenarioId,
    ServiceName,
    Severity,
)
from app.services.simulation import (
    API_GATEWAY,
    EVENTS_PER_TICK,
    MAX_TICK_INTERVAL_SECONDS,
    MAX_TICKS,
    ORDERS_DB,
    ORDERS_SERVICE,
    SimulationRun,
    SimulationSpec,
    run_simulation,
)

DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)
FAULT_START = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
LAST_VALID_START = datetime.max.replace(tzinfo=UTC) - timedelta(
    seconds=MAX_TICKS * MAX_TICK_INTERVAL_SECONDS
)
FAULT_SCENARIOS = [
    ScenarioId.DATABASE_LATENCY_SPIKE,
    ScenarioId.CPU_SATURATION,
    ScenarioId.API_ERRORS,
    ScenarioId.CONNECTION_FAILURE,
]


def _run(scenario: ScenarioId, seed: int = 7, ticks: int = 30) -> SimulationRun:
    """Run a simulation with a fixed seed so every assertion is reproducible."""
    return run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=ticks))


def _values(
    run: SimulationRun,
    service: ServiceName,
    metric: MetricName,
    *,
    anomalous: bool,
) -> list[float]:
    """Return values for one service and metric, filtered by ground-truth label."""
    return [
        event.value
        for event in run.events
        if event.service == service
        and event.metric_name == metric
        and event.label.is_anomalous is anomalous
    ]


def test_event_count_is_ticks_times_events_per_tick() -> None:
    run = _run(ScenarioId.NORMAL_OPERATION, ticks=12)

    assert len(run.events) == 12 * EVENTS_PER_TICK


def test_normal_operation_has_no_anomalies() -> None:
    run = _run(ScenarioId.NORMAL_OPERATION)

    assert run.anomaly_window is None
    assert run.anomalous_event_count == 0
    assert all(
        event.label.scenario_id is ScenarioId.NORMAL_OPERATION for event in run.events
    )


@pytest.mark.parametrize("scenario", FAULT_SCENARIOS)
def test_fault_is_labelled_only_inside_its_window(scenario: ScenarioId) -> None:
    run = _run(scenario, ticks=30)

    # 30 ticks -> fault active for ticks [10, 20).
    assert run.anomaly_window is not None
    assert run.anomaly_window.start == datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    assert run.anomaly_window.end == datetime(2026, 1, 1, 0, 20, tzinfo=UTC)
    for event in run.events:
        in_window = run.anomaly_window.start <= event.timestamp < run.anomaly_window.end
        assert event.label.is_anomalous is in_window
        assert event.label.scenario_id is scenario
    assert run.anomalous_event_count == 10 * EVENTS_PER_TICK


@pytest.mark.parametrize("scenario", FAULT_SCENARIOS)
def test_short_run_still_has_one_anomalous_tick(scenario: ScenarioId) -> None:
    run = _run(scenario, ticks=1)

    assert run.anomaly_window is not None
    assert run.anomalous_event_count == EVENTS_PER_TICK


def test_timestamps_follow_start_time_and_interval() -> None:
    start = datetime(2026, 3, 1, 12, tzinfo=timezone(timedelta(hours=-5)))
    spec = SimulationSpec(
        scenario=ScenarioId.NORMAL_OPERATION,
        seed=1,
        ticks=3,
        tick_interval_seconds=30,
        start_time=start,
    )

    run = run_simulation(spec)

    timestamps = sorted({event.timestamp for event in run.events})
    assert timestamps == [
        datetime(2026, 3, 1, 17, 0, tzinfo=UTC),
        datetime(2026, 3, 1, 17, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 1, 17, 1, tzinfo=UTC),
    ]


def test_same_spec_produces_identical_run() -> None:
    first = _run(ScenarioId.CPU_SATURATION, seed=42)
    second = _run(ScenarioId.CPU_SATURATION, seed=42)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_different_seed_changes_values_but_not_structure() -> None:
    first = _run(ScenarioId.API_ERRORS, seed=1)
    second = _run(ScenarioId.API_ERRORS, seed=2)

    assert [e.value for e in first.events] != [e.value for e in second.events]
    assert [(e.service, e.metric_name) for e in first.events] == [
        (e.service, e.metric_name) for e in second.events
    ]
    assert first.run_id != second.run_id


def test_scenarios_share_baseline_noise_for_the_same_seed() -> None:
    normal = _run(ScenarioId.NORMAL_OPERATION, seed=5)
    faulted = _run(ScenarioId.CPU_SATURATION, seed=5)

    # Before the fault window the two runs must be identical except for labels
    # and run identity, so differences in the window come from the fault alone.
    before = [e.value for e in normal.events if e.timestamp < FAULT_START]
    faulted_before = [e.value for e in faulted.events if e.timestamp < FAULT_START]
    assert before == faulted_before


def test_database_latency_spike_raises_db_and_request_latency() -> None:
    run = _run(ScenarioId.DATABASE_LATENCY_SPIKE, ticks=30)

    db_normal = mean(
        _values(run, ORDERS_DB, MetricName.DB_QUERY_LATENCY_MS, anomalous=False)
    )
    db_fault = mean(
        _values(run, ORDERS_DB, MetricName.DB_QUERY_LATENCY_MS, anomalous=True)
    )
    request_normal = mean(
        _values(run, API_GATEWAY, MetricName.REQUEST_LATENCY_MS, anomalous=False)
    )
    request_fault = mean(
        _values(run, API_GATEWAY, MetricName.REQUEST_LATENCY_MS, anomalous=True)
    )
    connections_fault = mean(
        _values(run, ORDERS_DB, MetricName.ACTIVE_DB_CONNECTIONS, anomalous=True)
    )

    assert db_fault > 5 * db_normal
    assert request_fault > 2 * request_normal
    assert connections_fault > 25


def test_cpu_saturation_drives_orders_service_cpu_high() -> None:
    run = _run(ScenarioId.CPU_SATURATION, ticks=30)

    cpu_fault = _values(
        run, ORDERS_SERVICE, MetricName.CPU_UTILIZATION_PERCENT, anomalous=True
    )
    cpu_normal = _values(
        run, ORDERS_SERVICE, MetricName.CPU_UTILIZATION_PERCENT, anomalous=False
    )

    assert min(cpu_fault) > 90.0
    assert mean(cpu_normal) < 50.0


def test_cpu_saturation_leaves_database_latency_near_baseline() -> None:
    run = _run(ScenarioId.CPU_SATURATION, ticks=30)

    db_fault = mean(
        _values(run, ORDERS_DB, MetricName.DB_QUERY_LATENCY_MS, anomalous=True)
    )

    assert db_fault < 20.0


def test_api_errors_raise_http_error_counts() -> None:
    run = _run(ScenarioId.API_ERRORS, ticks=30)

    errors_fault = _values(
        run, API_GATEWAY, MetricName.HTTP_ERROR_COUNT, anomalous=True
    )
    errors_normal = _values(
        run, API_GATEWAY, MetricName.HTTP_ERROR_COUNT, anomalous=False
    )

    assert mean(errors_fault) > 3.0
    assert mean(errors_normal) < 1.0
    assert all(value == int(value) for value in errors_fault)


def test_connection_failure_produces_db_connection_failures() -> None:
    run = _run(ScenarioId.CONNECTION_FAILURE, ticks=30)

    failures_fault = _values(
        run, ORDERS_SERVICE, MetricName.DB_CONNECTION_FAILURES, anomalous=True
    )
    connections_fault = _values(
        run, ORDERS_DB, MetricName.ACTIVE_DB_CONNECTIONS, anomalous=True
    )
    connections_normal = _values(
        run, ORDERS_DB, MetricName.ACTIVE_DB_CONNECTIONS, anomalous=False
    )

    assert mean(failures_fault) > 5.0
    assert mean(connections_fault) < 10.0
    assert mean(connections_normal) > 15.0


def test_normal_operation_has_rare_connection_failures() -> None:
    run = _run(ScenarioId.NORMAL_OPERATION, ticks=30)

    failures = _values(
        run, ORDERS_SERVICE, MetricName.DB_CONNECTION_FAILURES, anomalous=False
    )

    assert mean(failures) < 0.5


def test_every_value_is_finite_and_non_negative() -> None:
    run = _run(ScenarioId.CONNECTION_FAILURE, ticks=30)

    for event in run.events:
        assert math.isfinite(event.value)
        assert event.value >= 0.0


def test_run_id_is_derived_from_spec() -> None:
    first = _run(ScenarioId.API_ERRORS, seed=9)
    same_spec = _run(ScenarioId.API_ERRORS, seed=9)
    other_spec = _run(ScenarioId.API_ERRORS, seed=10)

    assert first.run_id == same_spec.run_id
    assert first.run_id != other_spec.run_id
    assert first.run_id.startswith("sim-")


@pytest.mark.parametrize(
    "overrides",
    [
        {"seed": -1},
        {"seed": 2**32},
        {"ticks": 0},
        {"ticks": MAX_TICKS + 1},
        {"tick_interval_seconds": 0},
        {"tick_interval_seconds": 3601},
        {"scenario": "no_such_scenario"},
        {"start_time": datetime(2026, 1, 1)},
        {"unexpected": True},
    ],
)
def test_spec_rejects_out_of_bounds_or_unknown_input(
    overrides: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "scenario": ScenarioId.CPU_SATURATION,
        "seed": 1,
        "ticks": 10,
    }
    payload.update(overrides)

    with pytest.raises(ValidationError):
        SimulationSpec.model_validate(payload)


def test_spec_normalizes_start_time_to_utc() -> None:
    spec = SimulationSpec(
        scenario=ScenarioId.NORMAL_OPERATION,
        seed=0,
        ticks=1,
        start_time=datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    )

    assert spec.start_time == datetime(2026, 1, 1, tzinfo=UTC)


def test_longest_run_at_last_valid_start_fits_the_calendar() -> None:
    run = run_simulation(
        SimulationSpec(
            scenario=ScenarioId.CPU_SATURATION,
            seed=1,
            ticks=MAX_TICKS,
            tick_interval_seconds=MAX_TICK_INTERVAL_SECONDS,
            start_time=LAST_VALID_START,
        )
    )

    last_timestamp = max(event.timestamp for event in run.events)
    assert last_timestamp == LAST_VALID_START + timedelta(
        seconds=(MAX_TICKS - 1) * MAX_TICK_INTERVAL_SECONDS
    )


def test_start_one_microsecond_past_last_valid_start_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SimulationSpec(
            scenario=ScenarioId.CPU_SATURATION,
            seed=1,
            ticks=MAX_TICKS,
            tick_interval_seconds=MAX_TICK_INTERVAL_SECONDS,
            start_time=LAST_VALID_START + timedelta(microseconds=1),
        )


def test_timeline_that_would_overflow_is_rejected_not_failed_later() -> None:
    # Two 60 s ticks starting at 23:59 on the last day would run past the end of
    # the calendar, so the spec must be refused before any run is generated.
    with pytest.raises(ValidationError):
        SimulationSpec(
            scenario=ScenarioId.NORMAL_OPERATION,
            seed=1,
            ticks=2,
            start_time=datetime(9999, 12, 31, 23, 59, tzinfo=UTC),
        )


def test_year_one_start_with_positive_offset_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SimulationSpec(
            scenario=ScenarioId.NORMAL_OPERATION,
            seed=1,
            ticks=1,
            start_time=datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        )


def test_earliest_utc_start_is_accepted() -> None:
    run = _run_from_start(datetime(1, 1, 1, tzinfo=UTC))

    assert min(event.timestamp for event in run.events) == datetime(
        1, 1, 1, tzinfo=UTC
    )


def _run_from_start(start: datetime) -> SimulationRun:
    """Run a short normal-operation simulation from an explicit start time."""
    return run_simulation(
        SimulationSpec(
            scenario=ScenarioId.NORMAL_OPERATION,
            seed=1,
            ticks=2,
            start_time=start,
        )
    )


def test_two_tick_run_places_fault_window_at_tick_zero() -> None:
    run = _run(ScenarioId.CPU_SATURATION, ticks=2)

    assert run.anomaly_window is not None
    assert run.anomaly_window.start == DEFAULT_START
    assert run.anomaly_window.end == datetime(2026, 1, 1, 0, 1, tzinfo=UTC)

    tick_zero = [event for event in run.events if event.timestamp == DEFAULT_START]
    tick_one = [event for event in run.events if event.timestamp != DEFAULT_START]
    assert len(tick_zero) == EVENTS_PER_TICK
    assert len(tick_one) == EVENTS_PER_TICK
    assert all(event.label.is_anomalous for event in tick_zero)
    assert not any(event.label.is_anomalous for event in tick_one)

    cpu_at_tick_zero = [
        event.value
        for event in tick_zero
        if event.metric_name is MetricName.CPU_UTILIZATION_PERCENT
        and event.service == ORDERS_SERVICE
    ]
    assert min(cpu_at_tick_zero) > 90.0


def test_anomalous_event_can_have_info_severity() -> None:
    """Labels are ground truth; severity classifies each value on its own."""
    run = _run(ScenarioId.DATABASE_LATENCY_SPIKE, ticks=30)

    info_latency = [
        event
        for event in run.events
        if event.label.is_anomalous
        and event.severity is Severity.INFO
        and event.metric_name is MetricName.REQUEST_LATENCY_MS
    ]
    assert info_latency
    warning_threshold = METRIC_SPECS[MetricName.REQUEST_LATENCY_MS].warning_threshold
    assert all(event.value < warning_threshold for event in info_latency)
