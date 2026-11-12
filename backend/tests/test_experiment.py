"""Leakage and reproducibility tests for the held-out detection experiment.

The runs come from the deterministic simulator. Training runs are fault-free and
end before the cutoff. Evaluation runs start at the cutoff and use seeds the
training never saw.
"""

from datetime import timedelta

import pytest

from app.domain.telemetry import ScenarioId, TelemetryEvent
from app.services.baseline import TrainEvaluationSplit
from app.services.detectors import ZScoreConfig, ZScoreDetector
from app.services.evaluation import ConfusionMatrix, WindowLevelReport
from app.services.experiment import (
    DetectorCandidate,
    run_experiment,
    select_detector,
    select_incident_rule,
)
from app.services.incident import ANY_POSITIVE_RULE, IncidentRule
from app.services.statistics import describe
from app.services.simulation import (
    DEFAULT_START_TIME,
    EVENTS_PER_TICK,
    SimulationSpec,
    run_simulation,
)

TICKS = 30
CUTOFF = DEFAULT_START_TIME + timedelta(minutes=TICKS)


def _split_and_events() -> tuple[TrainEvaluationSplit, list[TelemetryEvent]]:
    """One training run before the cutoff and two held-out runs after it."""
    training = run_simulation(
        SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=11, ticks=TICKS),
    )
    held_out_normal = run_simulation(
        SimulationSpec(
            scenario=ScenarioId.NORMAL_OPERATION,
            seed=12,
            ticks=TICKS,
            start_time=CUTOFF,
        ),
    )
    held_out_fault = run_simulation(
        SimulationSpec(
            scenario=ScenarioId.DATABASE_LATENCY_SPIKE,
            seed=13,
            ticks=TICKS,
            start_time=CUTOFF,
        ),
    )
    split = TrainEvaluationSplit(
        training_run_ids=frozenset({training.run_id}),
        evaluation_run_ids=frozenset({held_out_normal.run_id, held_out_fault.run_id}),
        cutoff=CUTOFF,
    )
    events = [*training.events, *held_out_normal.events, *held_out_fault.events]
    return split, events


def _detector() -> ZScoreDetector:
    """A fresh z-score detector with its default threshold."""
    return ZScoreDetector(ZScoreConfig())


def test_experiment_is_deterministic() -> None:
    split, events = _split_and_events()

    first = run_experiment(_detector(), split, events)
    second = run_experiment(_detector(), split, events)

    assert first == second


def test_training_uses_only_training_run_events() -> None:
    split, events = _split_and_events()

    report = run_experiment(_detector(), split, events)

    # The training run is fault-free, so every one of its 30 ticks contributes.
    assert report.training_event_count == TICKS * EVENTS_PER_TICK
    assert report.training_event_count == len(split.training_events(events))


def test_evaluation_uses_only_held_out_events_at_or_after_cutoff() -> None:
    split, events = _split_and_events()

    report = run_experiment(_detector(), split, events)

    assert report.evaluation_event_count == 2 * TICKS * EVENTS_PER_TICK
    assert report.evaluation_event_count == len(split.evaluation_events(events))


def test_changing_held_out_values_does_not_change_the_training_model() -> None:
    # The model is fitted on training events only. Scaling every held-out value
    # must leave the fitted model, and so its verdicts on held-out probes, unchanged.
    split, events = _split_and_events()
    shifted = [
        event
        if event.run_id in split.training_run_ids
        else event.model_copy(update={"value": event.value * 10.0})
        for event in events
    ]
    probes = split.evaluation_events(events)[:5]

    original = _detector()
    original.fit(split.training_events(events))
    perturbed = _detector()
    perturbed.fit(split.training_events(shifted))

    assert original.predict(probes) == perturbed.predict(probes)


def test_training_selection_never_includes_held_out_runs() -> None:
    split, events = _split_and_events()

    training = split.training_events(events)

    assert {event.run_id for event in training} <= split.training_run_ids
    assert all(event.timestamp < CUTOFF for event in training)


def test_metric_level_counts_cover_every_held_out_event() -> None:
    split, events = _split_and_events()

    report = run_experiment(_detector(), split, events)
    metric = report.metric_level

    # Every held-out event is scored, and each fault-window event is a positive.
    assert metric.unscored_count == 0
    assert metric.confusion.total == report.evaluation_event_count
    assert metric.confusion.tp + metric.confusion.fn == 10 * EVENTS_PER_TICK


def test_window_level_has_one_decision_per_held_out_run() -> None:
    split, events = _split_and_events()

    report = run_experiment(_detector(), split, events)

    assert report.window_level.confusion.total == 2
    assert report.window_level.unscored_run_count == 0
    # The fault run is the only anomalous run, so it is the only positive.
    assert report.window_level.confusion.tp + report.window_level.confusion.fn == 1


def test_report_names_the_detector() -> None:
    split, events = _split_and_events()

    report = run_experiment(_detector(), split, events)

    assert report.detector_name == "zscore"


def test_reusing_a_detector_refits_it_and_gives_the_same_report() -> None:
    split, events = _split_and_events()
    detector = _detector()

    first = run_experiment(detector, split, events)
    second = run_experiment(detector, split, events)

    assert first == second


def test_rule_changes_only_window_alerts_not_the_model_or_metric_levels() -> None:
    split, events = _split_and_events()
    strict = IncidentRule(min_affected_series=2, min_consecutive_ticks=2)

    baseline = run_experiment(_detector(), split, events)
    report = run_experiment(_detector(), split, events, strict)

    assert baseline.incident_rule == ANY_POSITIVE_RULE
    assert report.incident_rule == strict
    assert report.metric_level == baseline.metric_level
    assert report.effect_level == baseline.effect_level


def _report(tp: int, fp: int, tn: int, fn: int) -> WindowLevelReport:
    """A window-level report with the given run-level counts and no delays."""
    return WindowLevelReport(
        confusion=ConfusionMatrix(tp=tp, fp=fp, tn=tn, fn=fn),
        unscored_run_count=0,
        outcomes=(),
        delay_summary=describe([]),
        pre_incident_alert_run_count=0,
    )


def test_selection_takes_the_best_f1_among_rules_within_the_cap() -> None:
    weaker = IncidentRule(min_consecutive_ticks=2)
    best = IncidentRule(min_affected_series=2)
    reports = {
        weaker: _report(tp=8, fp=0, tn=10, fn=2),
        best: _report(tp=10, fp=0, tn=10, fn=0),
    }

    assert select_incident_rule(reports, max_run_false_positive_rate=0.1) == best


def test_rule_above_the_false_positive_cap_is_ineligible() -> None:
    eligible = IncidentRule(min_consecutive_ticks=2)
    over_cap = IncidentRule(min_consecutive_ticks=1)
    reports = {
        eligible: _report(tp=8, fp=0, tn=10, fn=2),
        over_cap: _report(tp=10, fp=2, tn=8, fn=0),
    }

    assert select_incident_rule(reports, max_run_false_positive_rate=0.1) == eligible


def test_f1_ties_go_to_the_lower_false_positive_rate() -> None:
    noisier = IncidentRule(min_affected_series=1)
    quieter = IncidentRule(min_affected_series=2)
    reports = {
        noisier: _report(tp=10, fp=1, tn=9, fn=0),
        quieter: _report(tp=10, fp=1, tn=19, fn=0),
    }

    assert select_incident_rule(reports, max_run_false_positive_rate=0.1) == quieter


def test_full_ties_go_to_the_less_demanding_rule() -> None:
    slower = IncidentRule(min_consecutive_ticks=3)
    faster = IncidentRule(min_consecutive_ticks=1)
    reports = {
        slower: _report(tp=10, fp=0, tn=10, fn=0),
        faster: _report(tp=10, fp=0, tn=10, fn=0),
    }

    assert select_incident_rule(reports, max_run_false_positive_rate=0.1) == faster


def test_undefined_false_positive_rate_is_not_eligible() -> None:
    # No normal runs means the false-positive rate cannot be checked, so the rule
    # cannot be selected on evidence of low false alarms.
    reports = {IncidentRule(): _report(tp=10, fp=0, tn=0, fn=0)}

    with pytest.raises(ValueError, match="no incident rule meets"):
        select_incident_rule(reports, max_run_false_positive_rate=0.1)


def test_no_rule_meeting_the_cap_is_an_error_not_a_silent_fallback() -> None:
    reports = {IncidentRule(): _report(tp=10, fp=5, tn=5, fn=0)}

    with pytest.raises(ValueError, match="no incident rule meets"):
        select_incident_rule(reports, max_run_false_positive_rate=0.1)


def test_healthy_run_alarms_under_any_positive_but_not_under_persistence() -> None:
    # Regression for the reported 1.0 run-level false-positive rate: a healthy
    # held-out run had isolated metric alarms, and any one of them made it a
    # false positive.
    split, events = _split_and_events()
    held_out = split.evaluation_events(events)
    faulty_runs = {event.run_id for event in held_out if event.label.is_anomalous}
    healthy_run = next(
        event.run_id for event in held_out if event.run_id not in faulty_runs
    )
    healthy_split = TrainEvaluationSplit(
        training_run_ids=split.training_run_ids,
        evaluation_run_ids=frozenset({healthy_run}),
        cutoff=CUTOFF,
    )
    persistent = IncidentRule(min_affected_series=2, min_consecutive_ticks=2)

    original = run_experiment(_detector(), healthy_split, events).window_level
    suppressed = run_experiment(
        _detector(), healthy_split, events, persistent
    ).window_level

    assert original.confusion == ConfusionMatrix(tp=0, fp=1, tn=0, fn=0)
    assert suppressed.confusion == ConfusionMatrix(tp=0, fp=0, tn=1, fn=0)


def _candidate(
    name: str,
    *,
    tp: int,
    fp: int,
    tn: int,
    fn: int,
    delay: float | None,
) -> DetectorCandidate:
    """A candidate whose validation report has the given counts and mean delay."""
    report = WindowLevelReport(
        confusion=ConfusionMatrix(tp=tp, fp=fp, tn=tn, fn=fn),
        unscored_run_count=0,
        outcomes=(),
        delay_summary=describe([] if delay is None else [delay]),
        pre_incident_alert_run_count=0,
    )
    return DetectorCandidate(name=name, rule=IncidentRule(), validation=report)


def test_detector_selection_takes_the_best_validation_f1_first() -> None:
    weaker = _candidate("weaker", tp=8, fp=2, tn=10, fn=2, delay=5.0)
    stronger = _candidate("stronger", tp=10, fp=0, tn=10, fn=0, delay=300.0)

    assert select_detector([weaker, stronger]).name == "stronger"


def test_detector_selection_breaks_f1_ties_by_shorter_validation_delay() -> None:
    slow = _candidate("slow", tp=40, fp=0, tn=10, fn=0, delay=94.5)
    fast = _candidate("fast", tp=40, fp=0, tn=10, fn=0, delay=9.0)

    assert select_detector([slow, fast]).name == "fast"


def test_detector_with_no_detected_window_ranks_last_on_delay() -> None:
    undefined = _candidate("undefined", tp=4, fp=0, tn=10, fn=0, delay=None)
    defined = _candidate("defined", tp=4, fp=0, tn=10, fn=0, delay=200.0)

    assert select_detector([undefined, defined]).name == "defined"


def test_detector_selection_full_ties_resolve_by_name() -> None:
    later = _candidate("b-detector", tp=4, fp=0, tn=10, fn=0, delay=10.0)
    earlier = _candidate("a-detector", tp=4, fp=0, tn=10, fn=0, delay=10.0)

    assert select_detector([later, earlier]).name == "a-detector"


def test_detector_selection_needs_at_least_one_candidate() -> None:
    with pytest.raises(ValueError, match="at least one"):
        select_detector([])
