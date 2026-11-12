"""Score the correlation window on synthetic validation seeds, with ground truth.

Baselines are normal runs from one seed set, checked by the offline synthetic
validation. The window is scored on a separate evaluation seed set, so the
baseline and the evaluation never share a run. Labels are read here, in the
offline scorer, and nowhere in the pipeline it scores.

    python scripts/evaluate_correlation_window.py

Defaults: baselines are seeds 101-104, and the evaluation is seeds 501-520, with
one normal run and one run per fault scenario for each seed, at 60 ticks. Seeds
501-520 were not used to choose the 600-second default. Seeds 401-420 are the
final held-out set and are not used by this script. Do not tune on this output
and then report it as held-out. The output is deterministic.

Results describe the simulator only. Its faults are large step changes, and its
normal behaviour is stationary. They say nothing about production telemetry.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.domain.telemetry import ScenarioId  # noqa: E402
from app.services.correlation_validation import WindowScore, score_windows  # noqa: E402
from app.services.offline_validation import BaselineRejected  # noqa: E402
from app.services.simulation import SimulationSpec, run_simulation  # noqa: E402

DEFAULT_WINDOWS = (60, 300, 600, 900, 1200)
DEFAULT_BASELINE_SEEDS = (101, 102, 103, 104)
DEFAULT_EVALUATION_SEEDS = tuple(range(501, 521))
DEFAULT_TICKS = 60


def _render(scores: Sequence[WindowScore]) -> str:
    """Format the window scores as a fixed-width table."""
    header = (
        f"{'window_s':>8} {'missed':>7} {'splits':>7} {'merges':>7} "
        f"{'false_inc':>10} {'incidents':>10}"
    )
    rows = [
        f"{score.window_seconds:>8} {score.missed_faults:>7} "
        f"{score.false_splits:>7} {score.false_merges:>7} "
        f"{score.false_incidents:>10} {score.incidents:>10}"
        for score in scores
    ]
    return "\n".join([header, *rows])


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sweep and print the table, or return non-zero if a baseline is faulty."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--baseline-seeds", type=int, nargs="+", default=list(DEFAULT_BASELINE_SEEDS)
    )
    parser.add_argument(
        "--evaluation-seeds", type=int, nargs="+", default=list(DEFAULT_EVALUATION_SEEDS)
    )
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--windows", type=int, nargs="+", default=list(DEFAULT_WINDOWS))
    args = parser.parse_args(argv)

    baselines = [
        run_simulation(
            SimulationSpec(
                scenario=ScenarioId.NORMAL_OPERATION,
                seed=seed,
                ticks=args.ticks,
            )
        )
        for seed in args.baseline_seeds
    ]
    evaluation = [
        run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=args.ticks))
        for seed in args.evaluation_seeds
        for scenario in ScenarioId
    ]
    try:
        scores = score_windows(baselines, evaluation, args.windows)
    except BaselineRejected as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1

    print(
        f"evaluation runs: {len(evaluation)} "
        f"({scores[0].fault_runs} fault, {scores[0].normal_runs} normal)"
    )
    print(_render(scores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
