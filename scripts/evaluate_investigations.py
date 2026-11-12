"""Evaluate investigation reports on synthetic validation seeds, with ground truth.

Baselines are normal runs from one seed set, checked by the offline synthetic
validation. Evaluation runs come from a separate seed set. Labels are read here,
in the offline scorer, and nowhere in the investigation it scores.

    python scripts/evaluate_investigations.py

Defaults: baselines are seeds 101-104, and the evaluation is seeds 601-610, with
one normal run and one run per fault scenario for each seed, at 60 ticks. Seeds
601-610 were not used to choose any rule or threshold. Do not tune the rules on
this output and then report the output as held-out. The output is deterministic.

Each incident is counted as in_window when its alert ticks overlap the window the
simulator labelled anomalous, and as a false positive otherwise. The total is
their sum. The detector is not tuned on these counts.

The report separates three kinds of check. Provenance, support, and
reproducibility test the report itself. Scenario agreement tests whether the
hypothesis that matches each simulator fault is generated, and whether it ranks
first. Agreement is not causal accuracy. The simulator's fault type is a label,
not a verified cause, and these numbers say nothing about production telemetry.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.domain.telemetry import ScenarioId  # noqa: E402
from app.services.investigation_evaluation import (  # noqa: E402
    RunScore,
    evaluate_investigations,
)
from app.services.offline_validation import BaselineRejected  # noqa: E402
from app.services.simulation import SimulationSpec, run_simulation  # noqa: E402

DEFAULT_BASELINE_SEEDS = (101, 102, 103, 104)
DEFAULT_EVALUATION_SEEDS = tuple(range(601, 611))
DEFAULT_TICKS = 60


@dataclass(frozen=True)
class ScenarioSummary:
    """Totals across the runs of one scenario."""

    scenario: ScenarioId
    runs: int
    incidents: int
    in_window: int
    false_positives: int
    provenance_failures: int
    unsupported_hypotheses: int
    irreproducible_runs: int
    expected_present_runs: int
    expected_ranked_first_runs: int
    contradictions: int
    missing_items: int


def summarize(scores: Sequence[RunScore]) -> list[ScenarioSummary]:
    """Group run scores by scenario, in the order the scenarios first appear."""
    order: list[ScenarioId] = []
    for score in scores:
        if score.scenario not in order:
            order.append(score.scenario)
    summaries: list[ScenarioSummary] = []
    for scenario in order:
        group = [score for score in scores if score.scenario is scenario]
        summaries.append(
            ScenarioSummary(
                scenario=scenario,
                runs=len(group),
                incidents=sum(score.incident_count for score in group),
                in_window=sum(score.window_incidents for score in group),
                false_positives=sum(
                    score.false_positive_incidents for score in group
                ),
                provenance_failures=sum(score.provenance_failures for score in group),
                unsupported_hypotheses=sum(
                    score.unsupported_hypotheses for score in group
                ),
                irreproducible_runs=sum(not score.reproducible for score in group),
                expected_present_runs=sum(
                    score.expected_present is True for score in group
                ),
                expected_ranked_first_runs=sum(
                    score.expected_ranked_first is True for score in group
                ),
                contradictions=sum(score.contradictions for score in group),
                missing_items=sum(score.missing_items for score in group),
            )
        )
    return summaries


def _render(summaries: Sequence[ScenarioSummary]) -> str:
    """Format the scenario summaries as a fixed-width table."""
    header = (
        f"{'scenario':<24} {'runs':>5} {'incidents':>10} {'in_window':>10} "
        f"{'false_pos':>10} {'prov_fail':>10} {'unsupp':>7} {'irrepro':>8} "
        f"{'present':>8} {'first':>6} {'contra':>7} {'missing':>8}"
    )
    rows = [
        f"{item.scenario.value:<24} {item.runs:>5} {item.incidents:>10} "
        f"{item.in_window:>10} {item.false_positives:>10} "
        f"{item.provenance_failures:>10} {item.unsupported_hypotheses:>7} "
        f"{item.irreproducible_runs:>8} {item.expected_present_runs:>8} "
        f"{item.expected_ranked_first_runs:>6} {item.contradictions:>7} "
        f"{item.missing_items:>8}"
        for item in summaries
    ]
    return "\n".join([header, *rows])


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluation and print the table, or return 1 if a baseline is faulty."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--baseline-seeds", type=int, nargs="+", default=list(DEFAULT_BASELINE_SEEDS)
    )
    parser.add_argument(
        "--evaluation-seeds",
        type=int,
        nargs="+",
        default=list(DEFAULT_EVALUATION_SEEDS),
    )
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
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
        scores = evaluate_investigations(baselines, evaluation)
    except BaselineRejected as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1

    print(f"evaluation runs: {len(evaluation)}")
    print(_render(summarize(scores)))
    print(
        "scenario agreement is not causal accuracy; see the module docstring "
        "in app/services/investigation_evaluation.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
