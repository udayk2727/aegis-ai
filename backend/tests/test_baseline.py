"""Tests for normal-operation baselines and the train/evaluation split."""

from datetime import timedelta, timezone

import pytest

from app.domain.telemetry import MetricName, ScenarioId
from app.services.baseline import TrainEvaluationSplit, build_baseline
from app.services.simulation import (
    DEFAULT_START_TIME,
    EVENTS_PER_TICK,
    SimulationRun,
    SimulationSpec,
    run_simulation,
)

# A 30-tick spike run has its fault window at ticks [10, 20): 90 anomalous events.
# The normal run below has 30 ticks and no anomalies.
CUTOFF = DEFAULT_START_TIME + timedelta(minutes=15)


def _spike_run() -> SimulationRun:
    """A fault run whose first ten ticks are normal and whose window is 10-19."""
    return run_simulation(
        SimulationSpec(
            scenario=ScenarioId.DATABASE_LATENCY_SPIKE,
            seed=7,
            ticks=30,
        ),
    )


def _normal_run() -> SimulationRun:
    """A fault-free run used as the held-out evaluation run."""
    return run_simulation(
        SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=3, ticks=30),
    )


def test_baseline_excludes_every_anomalous_event() -> None:
    run = _spike_run()

    baseline = build_baseline(run.events)

    assert baseline.sample_count == len(run.events) - run.anomalous_event_count
    assert baseline.sample_count == 20 * EVENTS_PER_TICK


def test_baseline_records_contributing_run_ids() -> None:
    spike = _spike_run()
    normal = _normal_run()

    baseline = build_baseline([*spike.events, *normal.events])

    assert baseline.source_run_ids == tuple(sorted([spike.run_id, normal.run_id]))


def test_baseline_of_only_anomalous_events_is_empty() -> None:
    spike = _spike_run()
    anomalous_only = [event for event in spike.events if event.label.is_anomalous]

    baseline = build_baseline(anomalous_only)

    assert baseline.sample_count == 0
    assert baseline.source_run_ids == ()
    assert baseline.series == ()


def test_baseline_series_have_no_anomalous_samples() -> None:
    run = _spike_run()

    baseline = build_baseline(run.events)
    db_latency = next(
        item
        for item in baseline.series
        if item.key.metric_name is MetricName.DB_QUERY_LATENCY_MS
    )

    # Ticks 0-9 and 20-29 are normal: 20 samples, none from the fault window.
    assert db_latency.stats.sample_count == 20


def test_split_rejects_overlapping_runs() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        TrainEvaluationSplit(
            training_run_ids=frozenset({"sim-0123456789abcdef"}),
            evaluation_run_ids=frozenset({"sim-0123456789abcdef"}),
            cutoff=CUTOFF,
        )


def test_split_requires_both_training_and_evaluation_runs() -> None:
    with pytest.raises(ValueError, match="training run"):
        TrainEvaluationSplit(
            training_run_ids=frozenset(),
            evaluation_run_ids=frozenset({"sim-0123456789abcdef"}),
            cutoff=CUTOFF,
        )
    with pytest.raises(ValueError, match="evaluation run"):
        TrainEvaluationSplit(
            training_run_ids=frozenset({"sim-0123456789abcdef"}),
            evaluation_run_ids=frozenset(),
            cutoff=CUTOFF,
        )


def test_split_rejects_naive_and_non_utc_cutoffs() -> None:
    naive = CUTOFF.replace(tzinfo=None)
    offset = CUTOFF.astimezone(timezone(timedelta(hours=2)))

    with pytest.raises(ValueError, match="timezone-aware"):
        TrainEvaluationSplit(
            training_run_ids=frozenset({"sim-0123456789abcdef"}),
            evaluation_run_ids=frozenset({"sim-fedcba9876543210"}),
            cutoff=naive,
        )
    with pytest.raises(ValueError, match="expressed in UTC"):
        TrainEvaluationSplit(
            training_run_ids=frozenset({"sim-0123456789abcdef"}),
            evaluation_run_ids=frozenset({"sim-fedcba9876543210"}),
            cutoff=offset,
        )


def test_training_events_are_normal_and_before_cutoff() -> None:
    spike = _spike_run()
    normal = _normal_run()
    split = TrainEvaluationSplit(
        training_run_ids=frozenset({spike.run_id}),
        evaluation_run_ids=frozenset({normal.run_id}),
        cutoff=CUTOFF,
    )

    training = split.training_events([*spike.events, *normal.events])

    # Minutes 0-14 cover ticks 0-14. Ticks 10-14 are anomalous and excluded, so
    # only ticks 0-9 (ten ticks of nine events) remain.
    assert len(training) == 10 * EVENTS_PER_TICK
    assert all(event.run_id == spike.run_id for event in training)
    assert all(event.timestamp < CUTOFF for event in training)
    assert not any(event.label.is_anomalous for event in training)


def test_evaluation_events_come_from_held_out_runs_at_or_after_cutoff() -> None:
    spike = _spike_run()
    normal = _normal_run()
    split = TrainEvaluationSplit(
        training_run_ids=frozenset({spike.run_id}),
        evaluation_run_ids=frozenset({normal.run_id}),
        cutoff=CUTOFF,
    )

    evaluation = split.evaluation_events([*spike.events, *normal.events])

    # Ticks 15-29 of the normal run start exactly at the cutoff and are kept.
    assert len(evaluation) == 15 * EVENTS_PER_TICK
    assert all(event.run_id == normal.run_id for event in evaluation)
    assert all(event.timestamp >= CUTOFF for event in evaluation)


def test_training_ends_before_evaluation_begins_and_runs_are_disjoint() -> None:
    spike = _spike_run()
    normal = _normal_run()
    split = TrainEvaluationSplit(
        training_run_ids=frozenset({spike.run_id}),
        evaluation_run_ids=frozenset({normal.run_id}),
        cutoff=CUTOFF,
    )
    everything = [*spike.events, *normal.events]

    training = split.training_events(everything)
    evaluation = split.evaluation_events(everything)

    assert not {e.run_id for e in training} & {e.run_id for e in evaluation}
    assert max(e.timestamp for e in training) < min(e.timestamp for e in evaluation)


def test_training_baseline_ignores_held_out_evaluation_run() -> None:
    spike = _spike_run()
    normal = _normal_run()
    split = TrainEvaluationSplit(
        training_run_ids=frozenset({spike.run_id}),
        evaluation_run_ids=frozenset({normal.run_id}),
        cutoff=CUTOFF,
    )

    with_evaluation = split.training_baseline([*spike.events, *normal.events])
    training_only = split.training_baseline(spike.events)

    assert with_evaluation == training_only
    assert with_evaluation.source_run_ids == (spike.run_id,)


def test_training_baseline_matches_training_events_alone() -> None:
    spike = _spike_run()
    normal = _normal_run()
    split = TrainEvaluationSplit(
        training_run_ids=frozenset({spike.run_id}),
        evaluation_run_ids=frozenset({normal.run_id}),
        cutoff=CUTOFF,
    )

    baseline = split.training_baseline([*spike.events, *normal.events])

    assert baseline == build_baseline(split.training_events([*spike.events]))
    assert baseline.sample_count == 10 * EVENTS_PER_TICK


def test_exploratory_baseline_does_include_evaluation_runs() -> None:
    # Documents the contrast: build_baseline is for exploration, and mixing
    # held-out runs into it is exactly what training_baseline prevents.
    spike = _spike_run()
    normal = _normal_run()

    exploratory = build_baseline([*spike.events, *normal.events])

    assert normal.run_id in exploratory.source_run_ids
