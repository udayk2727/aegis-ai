"""Tests for the investigation engine: observations, hypotheses, and reports.

Most tests run the real pipeline on simulator output. Tests of individual rules
use synthetic observations, so one rule can be exercised without relying on the
simulator producing that exact pattern.

The engine is label-blind. Labels are flipped in one test only, to show that the
report does not change when they do.
"""

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.incident_records import Incident
from app.domain.investigation_records import (
    CheckKind,
    HypothesisKind,
    HypothesisStatus,
    Investigation,
    InvestigationStatus,
    MissingEvidenceKind,
    ObservationKind,
    SeriesRef,
    VerifiedObservation,
)
from app.domain.telemetry import MetricName, ScenarioId
from app.services.baseline_trust import TrustedBaselines
from app.services.detection import Decision
from app.services.hypotheses import assess_hypotheses
from app.services.incident_pipeline import (
    SELECTED_RULE,
    ObservedRun,
    build_incidents,
    score_run,
)
from app.services.investigation import ANALYSIS_VERSION, investigate
from app.services.investigation_catalog import (
    CONNECTION_FAILURES,
    DB_QUERY_LATENCY,
    GATEWAY_ERRORS,
    ORDERINGS,
    ORDERS_CPU,
    ORDERS_ERRORS,
    REQUEST_LATENCY,
    RULES,
    WATCHED_SERIES,
)
from app.services.investigation_observations import observation_id
from app.services.simulation import SimulationRun
from tests.incident_factories import (
    EVALUATION_SEED,
    FAULT_SCENARIOS,
    baseline_runs,
    observe,
    simulate,
    trusted_baselines,
    with_flipped_labels,
)

BASELINE_IDS = tuple(sorted(run.run_id for run in baseline_runs()))
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
EXPECTED_BY_SCENARIO = {
    ScenarioId.DATABASE_LATENCY_SPIKE: (
        HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION
    ),
    ScenarioId.CPU_SATURATION: HypothesisKind.CPU_RESOURCE_SATURATION,
    ScenarioId.API_ERRORS: HypothesisKind.API_ERROR_PROPAGATION,
    ScenarioId.CONNECTION_FAILURE: HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,
}


def _observed(*runs: SimulationRun) -> list[ObservedRun]:
    """Present the approved baselines plus the given runs as plain values."""
    return [observe(run) for run in baseline_runs()] + [observe(run) for run in runs]


def _incidents_of(run: SimulationRun, observed: list[ObservedRun]) -> list[Incident]:
    """Return the incidents whose source run is the given run."""
    return [
        item
        for item in build_incidents(observed, BASELINE_IDS, trusted=trusted_baselines())
        if item.source_run_id == run.run_id
    ]


def _report(scenario: ScenarioId) -> tuple[Incident, Investigation]:
    """Build the only incident of one fault run and its report, on the test seed."""
    run = simulate(scenario, EVALUATION_SEED)
    observed = _observed(run)
    incidents = _incidents_of(run, observed)
    assert len(incidents) == 1
    verdicts = score_run(
        observed,
        BASELINE_IDS,
        run.run_id,
        trusted=trusted_baselines(),
    )
    return incidents[0], investigate(incidents[0], verdicts)


def _obs(
    kind: ObservationKind,
    series: tuple[SeriesRef, ...],
    *,
    event_ids: tuple[str, ...],
    start: datetime,
    end: datetime | None = None,
) -> VerifiedObservation:
    """Build a verified observation directly, for rule-level tests."""
    return VerifiedObservation(
        observation_id=observation_id(kind, series),
        kind=kind,
        series=series,
        statement="synthetic observation for a rule test.",
        source_event_ids=event_ids,
        source_event_count=len(event_ids),
        first_at=start,
        last_at=end or start,
    )


def _anomaly(
    series: SeriesRef,
    event_ids: tuple[str, ...],
    start: datetime = T0,
) -> VerifiedObservation:
    """A series anomalous at one alert tick per event ID, starting at ``start``."""
    return _obs(
        ObservationKind.ANOMALOUS_SERIES,
        (series,),
        event_ids=event_ids,
        start=start,
        end=start + timedelta(minutes=len(event_ids) - 1),
    )


def _normal(series: SeriesRef, event_ids: tuple[str, ...]) -> VerifiedObservation:
    """A series measured normal, citing the given event IDs."""
    return _obs(ObservationKind.NORMAL_SERIES, (series,), event_ids=event_ids, start=T0)


def _onset(
    earlier: VerifiedObservation,
    later: VerifiedObservation,
) -> VerifiedObservation:
    """The onset-order observation the pipeline records for two anomalies."""
    first, second = earlier.first_at, later.first_at
    return VerifiedObservation(
        observation_id=observation_id(
            ObservationKind.ONSET_ORDER,
            (earlier.series[0], later.series[0]),
        ),
        kind=ObservationKind.ONSET_ORDER,
        series=(earlier.series[0], later.series[0]),
        statement="synthetic onset order.",
        source_event_ids=(earlier.source_event_ids[0], later.source_event_ids[0]),
        source_event_count=2,
        first_at=min(first, second),
        last_at=max(first, second),
    )


def _hypothesis_of(
    kind: HypothesisKind,
    observations: list[VerifiedObservation],
) -> HypothesisKind | None:
    """Return the kind if it was generated from the observations, else None."""
    assessment = assess_hypotheses(observations)
    return next(
        (item.kind for item in assessment.hypotheses if item.kind is kind),
        None,
    )


def test_each_fault_scenario_generates_its_pattern() -> None:
    for scenario in FAULT_SCENARIOS:
        _, report = _report(scenario)

        kinds = {hypothesis.kind for hypothesis in report.hypotheses}
        assert EXPECTED_BY_SCENARIO[scenario] in kinds, scenario


def test_clear_scenarios_rank_their_pattern_first() -> None:
    for scenario in (ScenarioId.DATABASE_LATENCY_SPIKE, ScenarioId.CPU_SATURATION):
        _, report = _report(scenario)

        assert report.hypotheses[0].kind is EXPECTED_BY_SCENARIO[scenario], scenario


def test_normal_run_has_no_incident_to_investigate() -> None:
    normal = simulate(ScenarioId.NORMAL_OPERATION, EVALUATION_SEED)

    assert _incidents_of(normal, _observed(normal)) == []


def test_every_observation_cites_stored_events_of_its_run() -> None:
    run = simulate(ScenarioId.DATABASE_LATENCY_SPIKE, EVALUATION_SEED)
    _, report = _report(ScenarioId.DATABASE_LATENCY_SPIKE)
    events = {event.event_id: event for event in run.events}

    for observation in report.verified_observations:
        series = {(item.service, item.metric_name) for item in observation.series}
        for event_id in observation.source_event_ids:
            event = events[event_id]
            assert event.run_id == report.source_run_id
            assert (event.service, event.metric_name) in series
            assert report.analysis_span_start <= event.timestamp
            assert event.timestamp <= report.analysis_span_end


def test_normal_and_anomalous_observations_never_share_a_series() -> None:
    _, report = _report(ScenarioId.DATABASE_LATENCY_SPIKE)

    anomalous = {
        item.series[0]
        for item in report.verified_observations
        if item.kind is ObservationKind.ANOMALOUS_SERIES
    }
    normal = {
        item.series[0]
        for item in report.verified_observations
        if item.kind is ObservationKind.NORMAL_SERIES
    }
    assert anomalous and normal
    assert anomalous.isdisjoint(normal)


def test_anomalous_statements_give_baseline_scores_and_exclusions() -> None:
    _, report = _report(ScenarioId.DATABASE_LATENCY_SPIKE)

    latency = next(
        item
        for item in report.verified_observations
        if item.kind is ObservationKind.ANOMALOUS_SERIES
        and item.series[0] == DB_QUERY_LATENCY
    )
    assert "Detector scores ranged from" in latency.statement
    assert "against a threshold of 3" in latency.statement


def test_onset_order_records_database_before_latency() -> None:
    _, report = _report(ScenarioId.DATABASE_LATENCY_SPIKE)

    onsets = [
        item
        for item in report.verified_observations
        if item.kind is ObservationKind.ONSET_ORDER
    ]
    assert len(onsets) == 1
    assert onsets[0].series == (DB_QUERY_LATENCY, REQUEST_LATENCY)


def test_hypotheses_carry_uncertainty_and_stay_unverified() -> None:
    for scenario in FAULT_SCENARIOS:
        _, report = _report(scenario)

        for hypothesis in report.hypotheses:
            assert hypothesis.uncertainty
            assert hypothesis.verification_status.value == "unverified"
        assert report.root_cause_assessment.value == "not_assessed"


def test_alternatives_name_the_other_generated_hypotheses() -> None:
    _, report = _report(ScenarioId.API_ERRORS)

    kinds = {hypothesis.kind for hypothesis in report.hypotheses}
    for hypothesis in report.hypotheses:
        assert set(hypothesis.alternative_kinds) == kinds - {hypothesis.kind}


def test_report_is_deterministic_across_rebuilds() -> None:
    _, first = _report(ScenarioId.CONNECTION_FAILURE)
    _, second = _report(ScenarioId.CONNECTION_FAILURE)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.investigation_id.startswith("inv-")
    assert first.analysis_version == ANALYSIS_VERSION


def test_label_flip_does_not_change_the_report() -> None:
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    flipped = run.model_copy(update={"events": with_flipped_labels(run.events)})
    trusted = trusted_baselines()

    rendered = []
    for candidate in (run, flipped):
        observed = _observed(candidate)
        incident = _incidents_of(run, observed)[0]
        verdicts = score_run(observed, BASELINE_IDS, run.run_id, trusted=trusted)
        rendered.append(investigate(incident, verdicts).model_dump_json())

    assert rendered[0] == rendered[1]


def test_score_run_matches_the_alerts_behind_each_incident() -> None:
    run = simulate(ScenarioId.API_ERRORS, EVALUATION_SEED)
    observed = _observed(run)
    incidents = _incidents_of(run, observed)
    verdicts = score_run(
        observed, BASELINE_IDS, run.run_id, trusted=trusted_baselines()
    )
    alert_ticks = set(SELECTED_RULE.alert_times(verdicts))

    assert incidents
    for incident in incidents:
        assert incident.first_detection_at in alert_ticks
        assert incident.latest_detection_at in alert_ticks
        assert incident.alert_count <= len(alert_ticks)


def test_score_run_refuses_baseline_and_unknown_runs() -> None:
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    observed = _observed(run)
    trusted = trusted_baselines()

    with pytest.raises(ValueError, match="never scored"):
        score_run(observed, BASELINE_IDS, BASELINE_IDS[0], trusted=trusted)
    with pytest.raises(ValueError, match="run not found"):
        score_run(observed, BASELINE_IDS, "sim-0000000000000000", trusted=trusted)


def test_unapproved_baseline_is_refused_by_score_run() -> None:
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)

    with pytest.raises(ValueError, match="not approved"):
        score_run(_observed(run), BASELINE_IDS, run.run_id, trusted=TrustedBaselines())


def test_no_hypothesis_without_an_anomalous_observation() -> None:
    normal_only = [_normal(DB_QUERY_LATENCY, ("evt-db-1",))]

    assert assess_hypotheses(normal_only).hypotheses == ()


def test_single_alert_tick_does_not_generate_a_hypothesis() -> None:
    single = _anomaly(DB_QUERY_LATENCY, ("evt-db-1",))

    assessment = assess_hypotheses([single])

    assert _hypothesis_of(
        HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION,
        [single],
    ) is None
    assert MissingEvidenceKind.PRIMARY_SIGNAL_NOT_SUSTAINED in {
        item.kind
        for item in assessment.missing
        if HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION in item.affects
    }


def test_normal_corroboration_is_contradicting_evidence() -> None:
    db = _anomaly(DB_QUERY_LATENCY, ("evt-db-1", "evt-db-2"))
    latency_normal = _normal(REQUEST_LATENCY, ("evt-req-9",))

    assessment = assess_hypotheses([db, latency_normal])

    hypothesis = assessment.hypotheses[0]
    assert hypothesis.kind is HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION
    assert hypothesis.status is HypothesisStatus.SUPPORTED_WITH_CONTRADICTIONS
    assert hypothesis.contradicting_observation_ids == (latency_normal.observation_id,)
    contradiction = assessment.contradictions[0]
    assert contradiction.observation_id == latency_normal.observation_id
    assert contradiction.source_event_ids == ("evt-req-9",)


def test_latency_before_database_is_contradicting_evidence() -> None:
    db = _anomaly(DB_QUERY_LATENCY, ("evt-db-1", "evt-db-2"), start=T0)
    latency_first = _anomaly(
        REQUEST_LATENCY,
        ("evt-req-1", "evt-req-2"),
        start=T0 - timedelta(minutes=5),
    )

    assessment = assess_hypotheses([db, latency_first, _onset(db, latency_first)])

    hypothesis = assessment.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.SUPPORTED_WITH_CONTRADICTIONS
    ordering = [
        item for item in assessment.contradictions if "before" in item.explanation
    ]
    assert len(ordering) == 1
    assert ordering[0].observation_id.startswith("obs-onset_order")


def test_database_before_latency_is_supporting_evidence() -> None:
    db = _anomaly(DB_QUERY_LATENCY, ("evt-db-1", "evt-db-2"), start=T0)
    latency = _anomaly(
        REQUEST_LATENCY,
        ("evt-req-1", "evt-req-2"),
        start=T0 + timedelta(minutes=2),
    )

    assessment = assess_hypotheses([db, latency, _onset(db, latency)])

    hypothesis = assessment.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.SUPPORTED
    assert assessment.contradictions == ()
    assert any(
        observation_id.startswith("obs-onset_order")
        for observation_id in hypothesis.supporting_observation_ids
    )


def test_competing_anomaly_blocks_the_bottleneck_rule() -> None:
    latency = _anomaly(REQUEST_LATENCY, ("evt-req-1", "evt-req-2"))
    cpu = _anomaly(ORDERS_CPU, ("evt-cpu-1", "evt-cpu-2"))

    assessment = assess_hypotheses([latency, cpu])

    assert all(
        item.kind is not HypothesisKind.APPLICATION_PROCESSING_BOTTLENECK
        for item in assessment.hypotheses
    )
    assert MissingEvidenceKind.COMPETING_SIGNAL_NOT_RULED_OUT in {
        item.kind for item in assessment.missing
    }


def test_fewer_contradictions_rank_above_more_support() -> None:
    # The API rule has two anomalous error series, but its gateway corroboration
    # is measured normal. The connectivity rule has one anomalous series and no
    # contradiction. Fewer contradictions must rank first.
    api = _anomaly(ORDERS_ERRORS, ("evt-err-1", "evt-err-2"))
    gateway_normal = _normal(GATEWAY_ERRORS, ("evt-gw-1",))
    connectivity = _anomaly(CONNECTION_FAILURES, ("evt-conn-1", "evt-conn-2"))

    assessment = assess_hypotheses([api, gateway_normal, connectivity])

    ranking = [item.kind for item in assessment.hypotheses]
    assert ranking == [
        HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,
        HypothesisKind.API_ERROR_PROPAGATION,
    ]
    assert (
        assessment.hypotheses[1].status
        is HypothesisStatus.SUPPORTED_WITH_CONTRADICTIONS
    )


def test_ranking_does_not_depend_on_verdict_order() -> None:
    run = simulate(ScenarioId.CONNECTION_FAILURE, EVALUATION_SEED)
    observed = _observed(run)
    incident = _incidents_of(run, observed)[0]
    verdicts = list(
        score_run(observed, BASELINE_IDS, run.run_id, trusted=trusted_baselines())
    )
    expected = investigate(incident, verdicts).model_dump_json()
    random.Random(7).shuffle(verdicts)

    assert investigate(incident, verdicts).model_dump_json() == expected


def test_empty_observations_report_every_rule_as_not_sustained() -> None:
    assessment = assess_hypotheses([])

    assert assessment.hypotheses == ()
    assert len(assessment.missing) == len(RULES)
    assert {item.kind for item in assessment.missing} == {
        MissingEvidenceKind.PRIMARY_SIGNAL_NOT_SUSTAINED
    }


def test_without_verdicts_normal_checks_are_missing_not_assumed() -> None:
    run = simulate(ScenarioId.DATABASE_LATENCY_SPIKE, EVALUATION_SEED)
    incident = _incidents_of(run, _observed(run))[0]

    report = investigate(incident, ())

    assert report.status is InvestigationStatus.ANALYZED
    assert all(
        item.kind is not ObservationKind.NORMAL_SERIES
        for item in report.verified_observations
    )
    assert any(
        item.kind is MissingEvidenceKind.NO_MEASUREMENT_IN_RUN
        for item in report.missing_evidence
    )
    assert all(
        hypothesis.kind is not HypothesisKind.APPLICATION_PROCESSING_BOTTLENECK
        for hypothesis in report.hypotheses
    )


def test_unscored_measurements_are_reported_with_their_event_ids() -> None:
    run = simulate(ScenarioId.DATABASE_LATENCY_SPIKE, EVALUATION_SEED)
    observed = _observed(run)
    incident = _incidents_of(run, observed)[0]
    verdicts = list(
        score_run(observed, BASELINE_IDS, run.run_id, trusted=trusted_baselines())
    )
    target = next(
        index
        for index, item in enumerate(verdicts)
        if item.key.service == "orders-service"
        and item.key.metric_name is MetricName.CPU_UTILIZATION_PERCENT
        and incident.first_detection_at <= item.timestamp
    )
    verdicts[target] = replace(verdicts[target], decision=Decision.UNSCORED)

    report = investigate(incident, verdicts)

    unscored = [
        item
        for item in report.missing_evidence
        if item.kind is MissingEvidenceKind.UNSCORED_MEASUREMENTS
    ]
    assert len(unscored) == 1
    assert verdicts[target].event_id in unscored[0].source_event_ids
    # The unscored CPU series bears only on the CPU and bottleneck rules. Neither
    # is generated for a database spike, so no review check is suggested.
    assert CheckKind.REVIEW_UNSCORED_MEASUREMENTS not in {
        item.kind for item in report.suggested_checks
    }


def test_connectivity_report_notes_the_upper_tail_limit() -> None:
    _, report = _report(ScenarioId.CONNECTION_FAILURE)

    assert MissingEvidenceKind.DIRECTION_NOT_EVALUATED in {
        item.kind for item in report.missing_evidence
    }
    assert CheckKind.RESCORE_DOWNWARD_DEVIATIONS in {
        item.kind for item in report.suggested_checks
    }


def test_suggested_checks_are_read_only_and_bear_on_generated_hypotheses() -> None:
    for scenario in FAULT_SCENARIOS:
        _, report = _report(scenario)
        generated = {hypothesis.kind for hypothesis in report.hypotheses}

        for check in report.suggested_checks:
            assert check.read_only is True
            assert set(check.affects) <= generated


def test_missing_evidence_names_what_the_telemetry_model_does_not_collect() -> None:
    _, report = _report(ScenarioId.DATABASE_LATENCY_SPIKE)

    descriptions = " ".join(
        item.description
        for item in report.missing_evidence
        if item.kind is MissingEvidenceKind.NOT_IN_TELEMETRY_MODEL
    )
    assert "query-level traces" in descriptions
    assert "change records" in descriptions


def test_limitations_are_always_stated() -> None:
    _, report = _report(ScenarioId.CPU_SATURATION)

    text = " ".join(report.limitations).lower()
    assert "not" in text
    assert "cause" in text


def test_watched_series_cover_every_rule_and_every_ordering() -> None:
    watched = set(WATCHED_SERIES)

    for rule in RULES:
        assert set(rule.series) <= watched
    for earlier, later in ORDERINGS:
        assert {earlier, later} <= watched


def test_report_serializes_to_the_documented_fields() -> None:
    _, report = _report(ScenarioId.API_ERRORS)

    assert set(json.loads(report.model_dump_json())) == {
        "investigation_id",
        "analysis_version",
        "source_incident_id",
        "source_run_id",
        "baseline_run_ids",
        "analysis_span_start",
        "analysis_span_end",
        "status",
        "root_cause_assessment",
        "ranking_basis",
        "ranking_caveat",
        "verified_observations",
        "hypotheses",
        "contradictory_evidence",
        "missing_evidence",
        "suggested_checks",
        "limitations",
        "evidence_truncated",
    }

