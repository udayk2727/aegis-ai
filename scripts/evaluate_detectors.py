"""Evaluate the statistical detectors and incident rules on simulated incident data.

Each detector is fitted on fault-free training runs that end before a cutoff.
The run is staged so that each seed set is used for one purpose only:

- validation runs (seeds 301-310) choose the incident rule for each detector
  under a run-level false-positive cap, and choose the preferred detector between
  the two upper-tail candidates by validation F1, then validation delay;
- final runs (seeds 401-420) are scored once, with the configuration frozen
  above. Their results must not be used to tune anything.

Seeds 201-203 were used for exploratory checks before this staging. They are
retired from evaluation and do not appear here.

Every seed and parameter is fixed, so rerunning the script prints the same
tables. Only the standard library and the API package are used.

    python scripts/evaluate_detectors.py

Results are a function of the simulator. They show how the detectors behave on
synthetic, stationary data with step faults. They are not a claim about
production telemetry.
"""

import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.domain.telemetry import ScenarioId, TelemetryEvent  # noqa: E402
from app.services.baseline import TrainEvaluationSplit  # noqa: E402
from app.services.detection import AnomalyDetector, Direction  # noqa: E402
from app.services.detectors import (  # noqa: E402
    RobustConfig,
    RobustDetector,
    ZScoreConfig,
    ZScoreDetector,
)
from app.services.evaluation import (  # noqa: E402
    ConfusionMatrix,
    WindowLevelReport,
)
from app.services.experiment import (  # noqa: E402
    DetectorCandidate,
    ExperimentReport,
    run_experiment,
    select_detector,
    select_incident_rule,
)
from app.services.incident import ANY_POSITIVE_RULE, IncidentRule  # noqa: E402
from app.services.simulation import (  # noqa: E402
    DEFAULT_START_TIME,
    SimulationRun,
    SimulationSpec,
    run_simulation,
)

TICKS = 60
TRAINING_SEEDS = (101, 102, 103, 104)
VALIDATION_SEEDS = tuple(range(301, 311))
FINAL_SEEDS = tuple(range(401, 421))
FAULT_SCENARIOS = (
    ScenarioId.DATABASE_LATENCY_SPIKE,
    ScenarioId.CPU_SATURATION,
    ScenarioId.API_ERRORS,
    ScenarioId.CONNECTION_FAILURE,
)
CUTOFF = DEFAULT_START_TIME + timedelta(minutes=TICKS)
MAX_VALIDATION_RUN_FALSE_POSITIVE_RATE = 0.10
MIN_AFFECTED_SERIES_CHOICES = (1, 2, 3)
MIN_CONSECUTIVE_TICKS_CHOICES = (1, 2, 3, 5)
PREFERRED_DETECTOR = "zscore upper-tail"
COMPARISON_DETECTOR = "robust upper-tail"


def _normal_runs(seeds: Sequence[int], start_time: datetime) -> list[SimulationRun]:
    """Generate one fault-free run per seed, starting at ``start_time``."""
    return [
        run_simulation(
            SimulationSpec(
                scenario=ScenarioId.NORMAL_OPERATION,
                seed=seed,
                ticks=TICKS,
                start_time=start_time,
            ),
        )
        for seed in seeds
    ]


def _held_out_runs(seeds: Sequence[int]) -> list[SimulationRun]:
    """Generate one normal and one run per fault scenario for each seed."""
    return [
        run_simulation(
            SimulationSpec(
                scenario=scenario,
                seed=seed,
                ticks=TICKS,
                start_time=CUTOFF,
            ),
        )
        for seed in seeds
        for scenario in (ScenarioId.NORMAL_OPERATION, *FAULT_SCENARIOS)
    ]


def build_experiment(
    held_out_seeds: Sequence[int],
) -> tuple[TrainEvaluationSplit, list[TelemetryEvent]]:
    """Return the split and every generated event for one held-out seed set.

    Training runs are always the same normal runs before the cutoff. Held-out
    runs start at the cutoff, one normal run and one run per fault scenario for
    each seed. Each call returns only the events for its own held-out seeds, so
    the validation and final stages never share a run.
    """
    training_runs = _normal_runs(TRAINING_SEEDS, DEFAULT_START_TIME)
    held_out_runs = _held_out_runs(held_out_seeds)
    split = TrainEvaluationSplit(
        training_run_ids=frozenset(run.run_id for run in training_runs),
        evaluation_run_ids=frozenset(run.run_id for run in held_out_runs),
        cutoff=CUTOFF,
    )
    events = [
        event
        for run in (*training_runs, *held_out_runs)
        for event in run.events
    ]
    return split, events


def detector_configurations() -> dict[str, Callable[[], AnomalyDetector]]:
    """Return every detector configuration, keyed by name, in a fixed order."""
    return {
        "zscore two-sided": lambda: ZScoreDetector(ZScoreConfig()),
        "zscore upper-tail": lambda: ZScoreDetector(
            ZScoreConfig(direction=Direction.UPPER_TAIL)
        ),
        "robust two-sided": lambda: RobustDetector(RobustConfig()),
        "robust upper-tail": lambda: RobustDetector(
            RobustConfig(direction=Direction.UPPER_TAIL)
        ),
    }


def incident_rule_grid() -> list[IncidentRule]:
    """Return every incident rule the validation sweep compares, in a fixed order.

    The any-positive policy is the first entry, so the baseline is always part
    of the comparison.
    """
    return [
        IncidentRule(min_affected_series=series, min_consecutive_ticks=ticks)
        for ticks in MIN_CONSECUTIVE_TICKS_CHOICES
        for series in MIN_AFFECTED_SERIES_CHOICES
    ]


def _fmt(value: float | None) -> str:
    """Format a rate with three decimals, or n/a when it is undefined."""
    return "n/a" if value is None else f"{value:.3f}"


def _delay(report: WindowLevelReport) -> str:
    """Format the mean detection delay over detected windows, in seconds."""
    mean = report.delay_summary.mean
    return "n/a" if mean is None else f"{mean:.1f}"


RUN_HEADER = (
    f"{'rule':<26}{'TP':>4}{'FP':>4}{'TN':>4}{'FN':>4}"
    f"{'precision':>11}{'recall':>8}{'F1':>7}{'FPR':>7}{'delay s':>9}"
)


def _run_row(name: str, report: WindowLevelReport) -> str:
    """Format one run-level confusion matrix, its rates, and mean delay."""
    matrix = report.confusion
    return (
        f"{name:<26}{matrix.tp:>4}{matrix.fp:>4}{matrix.tn:>4}{matrix.fn:>4}"
        f"{_fmt(matrix.precision):>11}{_fmt(matrix.recall):>8}"
        f"{_fmt(matrix.f1):>7}{_fmt(matrix.false_positive_rate):>7}"
        f"{_delay(report):>9}"
    )


METRIC_HEADER = (
    f"{'level':<26}{'TP':>5}{'FP':>6}{'TN':>7}{'FN':>6}"
    f"{'precision':>11}{'recall':>9}{'F1':>8}{'FPR':>8}"
)


def _metric_row(name: str, matrix: ConfusionMatrix) -> str:
    """Format one event-level confusion matrix and its rates as a table row."""
    return (
        f"{name:<26}{matrix.tp:>5}{matrix.fp:>6}{matrix.tn:>7}{matrix.fn:>6}"
        f"{_fmt(matrix.precision):>11}{_fmt(matrix.recall):>9}"
        f"{_fmt(matrix.f1):>8}{_fmt(matrix.false_positive_rate):>8}"
    )


def _pre_incident_alerts(report: WindowLevelReport) -> int:
    """Total alerts raised before onset in fault runs. None of them detect."""
    return sum(outcome.pre_incident_alert_count for outcome in report.outcomes)


def print_validation_sweep(
    name: str,
    reports: Mapping[IncidentRule, ExperimentReport],
) -> None:
    """Print every candidate rule's run-level results on the validation runs."""
    print(f"\n== {name}: validation sweep (rule selection only) ==")
    print(RUN_HEADER)
    for rule, report in reports.items():
        print(_run_row(rule.name, report.window_level))


def print_selection(
    candidates: Sequence[DetectorCandidate],
    selected: DetectorCandidate,
) -> None:
    """Print the validation-only comparison that chose the preferred detector."""
    print("\n== detector selection (validation only) ==")
    print(f"{'detector':<22}{'rule':<26}{'F1':>7}{'delay s':>9}")
    for candidate in candidates:
        f1 = candidate.validation.confusion.f1
        print(
            f"{candidate.name:<22}{candidate.rule.name:<26}"
            f"{_fmt(f1):>7}{_delay(candidate.validation):>9}"
        )
    print(
        f"selected: {selected.name} with {selected.rule.name} "
        "(highest validation F1, then shortest validation delay)"
    )


def print_final(
    name: str,
    rule: IncidentRule,
    frozen: ExperimentReport,
    baseline: ExperimentReport,
) -> None:
    """Print the single final evaluation of one frozen detector.

    Missed incidents are the false negatives. Pre-incident alerts are alerts
    raised before onset in fault runs, reported separately and never counted as
    detections.
    """
    window = frozen.window_level
    matrix = window.confusion
    fault_runs = matrix.tp + matrix.fn
    print(f"\n== {name}: final runs, configuration frozen on validation ==")
    print(
        f"training events: {frozen.training_event_count}, "
        f"final events: {frozen.evaluation_event_count}, "
        f"unscored events: {frozen.metric_level.unscored_count}"
    )
    print(f"frozen rule: {rule.name}")
    print(RUN_HEADER)
    print(_run_row("frozen", window))
    print(_run_row("any-positive (baseline)", baseline.window_level))
    print(
        f"missed incidents (FN): {matrix.fn} of {fault_runs} fault runs; "
        f"pre-incident alerts: {_pre_incident_alerts(window)} across "
        f"{window.pre_incident_alert_run_count} fault runs "
        "(not counted as detections)"
    )
    print(METRIC_HEADER)
    print(_metric_row("metric level (labelled)", frozen.metric_level.confusion))
    print(_metric_row("metric level (affected)", frozen.effect_level))


def main() -> None:
    """Sweep rules on validation, select the preferred detector, then score once."""
    validation_split, validation_events = build_experiment(VALIDATION_SEEDS)
    validation_count = len(VALIDATION_SEEDS) * (len(FAULT_SCENARIOS) + 1)
    final_count = len(FINAL_SEEDS) * (len(FAULT_SCENARIOS) + 1)
    print(
        f"Training: {len(TRAINING_SEEDS)} normal runs before the cutoff. "
        f"Validation: {validation_count} runs, used only for rule and detector "
        f"selection. Final: {final_count} runs, scored once with frozen settings."
    )
    print(
        "Labelled truth is the scenario window. Affected truth is the window "
        "restricted to series the fault changes. Run-level truth is one decision "
        "per evaluation run."
    )

    configurations = detector_configurations()
    rules = incident_rule_grid()
    candidates: list[DetectorCandidate] = []
    for name, make_detector in configurations.items():
        validation_reports = {
            rule: run_experiment(
                make_detector(), validation_split, validation_events, rule
            )
            for rule in rules
        }
        print_validation_sweep(name, validation_reports)
        chosen = select_incident_rule(
            {rule: report.window_level for rule, report in validation_reports.items()},
            MAX_VALIDATION_RUN_FALSE_POSITIVE_RATE,
        )
        if name in (PREFERRED_DETECTOR, COMPARISON_DETECTOR):
            candidates.append(
                DetectorCandidate(
                    name=name,
                    rule=chosen,
                    validation=validation_reports[chosen].window_level,
                )
            )

    selected = select_detector(candidates)
    print_selection(candidates, selected)

    final_split, final_events = build_experiment(FINAL_SEEDS)
    for candidate in sorted(candidates, key=lambda item: item.name != selected.name):
        make_detector = configurations[candidate.name]
        frozen = run_experiment(
            make_detector(), final_split, final_events, candidate.rule
        )
        baseline = run_experiment(
            make_detector(), final_split, final_events, ANY_POSITIVE_RULE
        )
        role = "preferred" if candidate.name == selected.name else "comparison"
        print_final(f"{candidate.name} ({role})", candidate.rule, frozen, baseline)


if __name__ == "__main__":
    main()
