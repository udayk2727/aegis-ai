"""Tests for the offline investigation evaluation, using a small deterministic set.

The evaluation reads labels, so these tests check the evaluator as well as the
reports it scores. The invariants are the point: provenance, support, and
reproducibility must hold for every run. Scenario agreement is asserted only as
presence. Ranking is reported by the script, not asserted here, because the
connection-failure ranking is a known weakness and the script reports it.
"""

import pytest

from app.domain.investigation_records import (
    HypothesisKind,
    Investigation,
    MissingEvidenceKind,
    ObservationKind,
)
from app.domain.telemetry import ScenarioId
from app.services.baseline_trust import TrustedBaselines
from app.services.incident_pipeline import ObservedRun, build_incidents, score_run
from app.services.investigation import investigate
from app.services.investigation_evaluation import (
    EXPECTED_HYPOTHESIS,
    RunScore,
    evaluate_investigations,
    provenance_failures,
    unsupported_hypotheses,
)
from app.services.offline_validation import BaselineRejected, approve_baseline_specs
from app.services.simulation import SimulationRun, SimulationSpec, run_simulation

BASELINE_SEEDS = (101, 102, 103, 104)
EVALUATION_SEEDS = (601, 602, 603)
TICKS = 60


def _baselines() -> list[SimulationRun]:
    """Fault-free runs used as the normal behaviour for every evaluation here."""
    return [
        run_simulation(
            SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=seed, ticks=TICKS)
        )
        for seed in BASELINE_SEEDS
    ]


@pytest.fixture(scope="module")
def scores() -> list[RunScore]:
    """Score the small evaluation set once for the module."""
    evaluation = [
        run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=TICKS))
        for seed in EVALUATION_SEEDS
        for scenario in ScenarioId
    ]
    return evaluate_investigations(_baselines(), evaluation)


def _report_for(scenario: ScenarioId) -> tuple[SimulationRun, Investigation]:
    """Build one real report, for the provenance and support checks to examine."""
    baselines = _baselines()
    run = run_simulation(SimulationSpec(scenario=scenario, seed=601, ticks=TICKS))
    trusted = TrustedBaselines(
        tuple(
            approve_baseline_specs(
                SimulationSpec(
                    scenario=ScenarioId.NORMAL_OPERATION,
                    seed=seed,
                    ticks=TICKS,
                )
                for seed in BASELINE_SEEDS
            )
        )
    )
    baseline_ids = [item.run_id for item in baselines]
    observed = [
        ObservedRun(item.run_id, item.spec.tick_interval_seconds, item.events)
        for item in [*baselines, run]
    ]
    incident = next(
        item
        for item in build_incidents(observed, baseline_ids, trusted=trusted)
        if item.source_run_id == run.run_id
    )
    verdicts = score_run(observed, baseline_ids, run.run_id, trusted=trusted)
    return run, investigate(incident, verdicts)


def test_every_fault_scenario_has_an_expected_hypothesis() -> None:
    assert set(EXPECTED_HYPOTHESIS) == {
        scenario
        for scenario in ScenarioId
        if scenario is not ScenarioId.NORMAL_OPERATION
    }


def test_no_run_has_a_provenance_failure(scores: list[RunScore]) -> None:
    assert all(score.provenance_failures == 0 for score in scores)


def test_every_hypothesis_has_non_normal_support(scores: list[RunScore]) -> None:
    assert all(score.unsupported_hypotheses == 0 for score in scores)


def test_every_run_is_reproducible(scores: list[RunScore]) -> None:
    assert all(score.reproducible for score in scores)


def test_normal_runs_produce_no_investigation(scores: list[RunScore]) -> None:
    normal = [
        score for score in scores if score.scenario is ScenarioId.NORMAL_OPERATION
    ]

    assert normal
    assert all(score.incident_count == 0 for score in normal)
    assert all(score.expected_present is None for score in normal)


def test_each_fault_run_reports_its_expected_pattern(scores: list[RunScore]) -> None:
    faults = [
        score for score in scores if score.scenario is not ScenarioId.NORMAL_OPERATION
    ]

    assert faults
    assert all(score.incident_count >= 1 for score in faults)
    assert all(score.expected_present is True for score in faults)


def test_every_fault_run_reports_missing_evidence(scores: list[RunScore]) -> None:
    faults = [
        score for score in scores if score.scenario is not ScenarioId.NORMAL_OPERATION
    ]

    assert all(score.missing_items > 0 for score in faults)


def test_baseline_that_contains_a_fault_stops_the_evaluation() -> None:
    faulty = run_simulation(
        SimulationSpec(scenario=ScenarioId.CPU_SATURATION, seed=101, ticks=TICKS)
    )

    with pytest.raises(BaselineRejected):
        evaluate_investigations([faulty], [])


def test_provenance_check_catches_an_event_id_that_does_not_exist() -> None:
    run, report = _report_for(ScenarioId.CPU_SATURATION)
    events = {event.event_id: event for event in run.events}
    assert provenance_failures(report, events) == 0

    first = report.verified_observations[0]
    forged = first.model_copy(
        update={"source_event_ids": ("evt-does-not-exist", *first.source_event_ids[1:])}
    )
    tampered = report.model_copy(
        update={"verified_observations": (forged, *report.verified_observations[1:])}
    )

    assert provenance_failures(tampered, events) == 1


def test_provenance_check_catches_an_event_from_another_series() -> None:
    run, report = _report_for(ScenarioId.CPU_SATURATION)
    anomalous = next(
        item
        for item in report.verified_observations
        if item.kind is ObservationKind.ANOMALOUS_SERIES
    )
    series = (anomalous.series[0].service, anomalous.series[0].metric_name)
    other = next(
        event
        for event in run.events
        if (event.service, event.metric_name) != series
        and report.analysis_span_start <= event.timestamp <= report.analysis_span_end
    )
    forged = anomalous.model_copy(update={"source_event_ids": (other.event_id,)})
    tampered = report.model_copy(
        update={
            "verified_observations": tuple(
                forged if item is anomalous else item
                for item in report.verified_observations
            )
        }
    )

    events = {event.event_id: event for event in run.events}
    assert provenance_failures(tampered, events) >= 1


def test_support_check_counts_a_hypothesis_backed_only_by_normal_data() -> None:
    _, report = _report_for(ScenarioId.CPU_SATURATION)
    # Onset order is derived from anomalies, so it counts as anomaly-based support
    # and must be relabelled with them. The copy skips validation, which is
    # acceptable here because only the support check reads the relabelled kinds.
    anomaly_based = {ObservationKind.ANOMALOUS_SERIES, ObservationKind.ONSET_ORDER}
    relabelled = report.model_copy(
        update={
            "verified_observations": tuple(
                item.model_copy(update={"kind": ObservationKind.NORMAL_SERIES})
                if item.kind in anomaly_based
                else item
                for item in report.verified_observations
            )
        }
    )

    assert unsupported_hypotheses(relabelled) == len(relabelled.hypotheses)


def test_missing_evidence_names_the_telemetry_gaps() -> None:
    _, report = _report_for(ScenarioId.DATABASE_LATENCY_SPIKE)

    kinds = {item.kind for item in report.missing_evidence}
    assert MissingEvidenceKind.NOT_IN_TELEMETRY_MODEL in kinds
    assert HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION in {
        kind for item in report.missing_evidence for kind in item.affects
    }
