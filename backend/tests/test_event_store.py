"""Tests for the bounded in-memory event store."""

import pytest

from app.domain.telemetry import MetricName, ScenarioId
from app.services.event_store import EventFilter, EventStore, RunTooLargeError
from app.services.simulation import (
    EVENTS_PER_TICK,
    SimulationRun,
    SimulationSpec,
    run_simulation,
)


def _run(scenario: ScenarioId, seed: int, ticks: int = 3) -> SimulationRun:
    """Build a small run so the store tests stay fast and readable."""
    return run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=ticks))


def test_rejects_capacity_below_one() -> None:
    with pytest.raises(ValueError):
        EventStore(max_events=0)


def test_add_run_returns_true_then_false_for_duplicate() -> None:
    store = EventStore(max_events=1000)
    run = _run(ScenarioId.NORMAL_OPERATION, seed=1)

    assert store.add_run(run) is True
    assert store.add_run(run) is False

    _, total = store.query(EventFilter(), offset=0, limit=1)
    assert total == len(run.events)


def test_rejects_run_larger_than_capacity_without_storing_it() -> None:
    store = EventStore(max_events=EVENTS_PER_TICK)
    run = _run(ScenarioId.NORMAL_OPERATION, seed=1, ticks=2)

    with pytest.raises(RunTooLargeError) as raised:
        store.add_run(run)

    assert raised.value.event_count == len(run.events)
    _, total = store.query(EventFilter(), offset=0, limit=1)
    assert total == 0


def test_evicts_oldest_whole_runs_when_full() -> None:
    first = _run(ScenarioId.NORMAL_OPERATION, seed=1)
    second = _run(ScenarioId.NORMAL_OPERATION, seed=2)
    third = _run(ScenarioId.NORMAL_OPERATION, seed=3)
    store = EventStore(max_events=len(first.events) * 2)

    store.add_run(first)
    store.add_run(second)
    store.add_run(third)

    _, total = store.query(EventFilter(), offset=0, limit=1)
    assert total == len(second.events) + len(third.events)
    run_ids = {
        event.run_id
        for event in store.query(EventFilter(), offset=0, limit=10_000)[0]
    }
    assert run_ids == {second.run_id, third.run_id}


def test_evicted_run_can_be_stored_again() -> None:
    first = _run(ScenarioId.NORMAL_OPERATION, seed=1)
    second = _run(ScenarioId.NORMAL_OPERATION, seed=2)
    store = EventStore(max_events=len(first.events))

    store.add_run(first)
    store.add_run(second)

    assert store.add_run(first) is True


def test_query_filters_by_each_criterion() -> None:
    normal = _run(ScenarioId.NORMAL_OPERATION, seed=1)
    faulted = _run(ScenarioId.API_ERRORS, seed=1)
    store = EventStore(max_events=10_000)
    store.add_run(normal)
    store.add_run(faulted)

    def total(event_filter: EventFilter) -> int:
        return store.query(event_filter, offset=0, limit=1)[1]

    assert total(EventFilter(run_id=faulted.run_id)) == len(faulted.events)
    assert total(EventFilter(scenario_id=ScenarioId.API_ERRORS)) == len(faulted.events)
    assert total(EventFilter(is_anomalous=True)) == faulted.anomalous_event_count
    assert total(EventFilter(is_anomalous=False)) == (
        len(normal.events) + len(faulted.events) - faulted.anomalous_event_count
    )
    assert total(EventFilter(service="orders-db")) == 3 * 3 * 2
    assert total(EventFilter(metric_name=MetricName.HTTP_ERROR_COUNT)) == 3 * 2 * 2


def test_query_paginates_in_insertion_order() -> None:
    run = _run(ScenarioId.NORMAL_OPERATION, seed=4)
    store = EventStore(max_events=10_000)
    store.add_run(run)

    page, total = store.query(EventFilter(), offset=5, limit=4)

    assert total == len(run.events)
    assert [event.event_id for event in page] == [
        event.event_id for event in run.events[5:9]
    ]


def test_query_past_the_end_returns_empty_page_with_total() -> None:
    run = _run(ScenarioId.NORMAL_OPERATION, seed=4)
    store = EventStore(max_events=10_000)
    store.add_run(run)

    page, total = store.query(EventFilter(), offset=10_000, limit=10)

    assert page == []
    assert total == len(run.events)


def test_runs_snapshot_is_oldest_first_and_unaffected_by_later_writes() -> None:
    store = EventStore(max_events=1000)
    first = _run(ScenarioId.NORMAL_OPERATION, seed=1)
    second = _run(ScenarioId.CPU_SATURATION, seed=2)
    store.add_run(first)

    snapshot = store.runs()
    store.add_run(second)

    assert [run.run_id for run in snapshot] == [first.run_id]
    assert [run.run_id for run in store.runs()] == [first.run_id, second.run_id]
