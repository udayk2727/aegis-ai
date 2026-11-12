"""Tests for the offline correlation-window scorer."""

import pytest

from app.domain.telemetry import ScenarioId
from app.services.correlation_validation import score_windows
from app.services.offline_validation import BaselineRejected
from app.services.simulation import SimulationRun, SimulationSpec, run_simulation

TICKS = 60
WINDOW = 600


def _run(scenario: ScenarioId, seed: int) -> SimulationRun:
    return run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=TICKS))


def _evaluation() -> list[SimulationRun]:
    return [
        _run(scenario, seed)
        for seed in (301, 302)
        for scenario in ScenarioId
    ]


def test_a_faulty_baseline_stops_the_scorer() -> None:
    faulty = _run(ScenarioId.CPU_SATURATION, 101)

    with pytest.raises(BaselineRejected):
        score_windows([faulty], _evaluation(), [WINDOW])


def test_scores_count_every_evaluation_run_and_keep_window_order() -> None:
    baselines = [_run(ScenarioId.NORMAL_OPERATION, 101)]

    scores = score_windows(baselines, _evaluation(), [900, WINDOW])

    assert [score.window_seconds for score in scores] == [900, WINDOW]
    for score in scores:
        assert score.fault_runs == 8
        assert score.normal_runs == 2


def test_the_default_window_misses_no_fault_and_raises_no_false_alarm_here() -> None:
    baselines = [_run(ScenarioId.NORMAL_OPERATION, 101)]

    (score,) = score_windows(baselines, _evaluation(), [WINDOW])

    assert score.missed_faults == 0
    assert score.false_splits == 0
    assert score.false_merges == 0
    assert score.false_incidents == 0


def test_scoring_is_deterministic() -> None:
    baselines = [_run(ScenarioId.NORMAL_OPERATION, 101)]

    first = score_windows(baselines, _evaluation(), [60, WINDOW])
    second = score_windows(baselines, _evaluation(), [60, WINDOW])

    assert first == second
