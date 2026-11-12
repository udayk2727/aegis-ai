"""Held-out experiment: fit on training runs, score separate evaluation runs.

``run_experiment`` is the only sanctioned path from events to a detector score.
It enforces the leakage rules in one place:

1. The training set comes from ``TrainEvaluationSplit.training_events``. It holds
   normal-labelled events from training runs, strictly before the cutoff.
2. The detector is fitted on that set and nothing else.
3. The evaluation set comes from held-out runs, at or after the cutoff.
4. Predictions are made before any label is read. Labels are used only when the
   predictions are scored.

The detector receives events that still carry their labels, because the event
type holds them. The detector is written never to read them, and a test checks
that flipping labels does not change its predictions.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.domain.telemetry import TelemetryEvent
from app.services.baseline import TrainEvaluationSplit
from app.services.detection import AnomalyDetector
from app.services.evaluation import (
    ConfusionMatrix,
    MetricLevelReport,
    WindowLevelReport,
    evaluate_effect_level,
    evaluate_metric_level,
    evaluate_window_level,
)
from app.services.incident import ANY_POSITIVE_RULE, IncidentRule


@dataclass(frozen=True)
class ExperimentReport:
    """Results of one detector on one split, at each evaluation level.

    ``incident_rule`` is the rule that produced the window-level alerts. The
    effect level scores each event against whether its series was affected.
    """

    detector_name: str
    training_event_count: int
    evaluation_event_count: int
    incident_rule: IncidentRule
    metric_level: MetricLevelReport
    effect_level: ConfusionMatrix
    window_level: WindowLevelReport


def run_experiment(
    detector: AnomalyDetector,
    split: TrainEvaluationSplit,
    events: Iterable[TelemetryEvent],
    rule: IncidentRule = ANY_POSITIVE_RULE,
) -> ExperimentReport:
    """Fit ``detector`` on training data, then score it on held-out data.

    The same ``events`` may hold training and evaluation runs together, because
    the split selects each side. The detector is refitted from scratch on every
    call, so an experiment never inherits state from an earlier one. ``rule``
    only changes how predictions are aggregated into alerts. It cannot change
    the fitted model.
    """
    all_events = list(events)
    training = split.training_events(all_events)
    held_out = split.evaluation_events(all_events)

    detector.fit(training)
    predictions = detector.predict(held_out)

    return ExperimentReport(
        detector_name=detector.name,
        training_event_count=len(training),
        evaluation_event_count=len(held_out),
        incident_rule=rule,
        metric_level=evaluate_metric_level(held_out, predictions),
        effect_level=evaluate_effect_level(held_out, predictions),
        window_level=evaluate_window_level(held_out, predictions, rule),
    )


def select_incident_rule(
    validation_reports: Mapping[IncidentRule, WindowLevelReport],
    max_run_false_positive_rate: float,
) -> IncidentRule:
    """Pick the rule with the best validation window F1 under a false-alarm cap.

    Pass validation reports only. Test reports must never reach this function,
    so the final evaluation stays untouched by the choice. Rules whose run-level
    false-positive rate is above the cap, or undefined, are ineligible. Among the
    rest the highest F1 wins. Ties go to the lower false-positive rate, then to
    the less demanding rule, so the choice is deterministic.

    Raises ValueError when no rule meets the cap. Relaxing the cap silently would
    hide that the detector cannot meet it.
    """
    eligible: list[tuple[tuple[float, float, int, int], IncidentRule]] = []
    for rule, report in validation_reports.items():
        false_positive_rate = report.confusion.false_positive_rate
        if false_positive_rate is None:
            continue
        if false_positive_rate > max_run_false_positive_rate:
            continue
        f1 = report.confusion.f1 or 0.0
        rank = (
            -f1,
            false_positive_rate,
            rule.min_consecutive_ticks,
            rule.min_affected_series,
        )
        eligible.append((rank, rule))
    if not eligible:
        raise ValueError("no incident rule meets the run-level false-positive cap")
    return min(eligible, key=lambda item: item[0])[1]


@dataclass(frozen=True)
class DetectorCandidate:
    """A detector configuration with the incident rule chosen for it on validation.

    ``validation`` is the window-level report under ``rule``, computed on
    validation runs only. It is the only input ``select_detector`` reads.
    """

    name: str
    rule: IncidentRule
    validation: WindowLevelReport


def select_detector(candidates: Sequence[DetectorCandidate]) -> DetectorCandidate:
    """Pick the candidate with the best validation window F1, then the shortest delay.

    Validation F1 is compared first. An undefined F1 counts as 0. Ties go to the
    lower mean validation detection delay, where a candidate with no detected
    window ranks last. Remaining ties go to the name, so the choice is
    deterministic. Pass validation reports only. Test reports must never reach
    this function.
    """
    if not candidates:
        raise ValueError("at least one detector candidate is required")

    def rank(candidate: DetectorCandidate) -> tuple[float, float, str]:
        f1 = candidate.validation.confusion.f1 or 0.0
        delay = candidate.validation.delay_summary.mean
        return (-f1, math.inf if delay is None else delay, candidate.name)

    return min(candidates, key=rank)
