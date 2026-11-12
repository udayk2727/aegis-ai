"""Regression tests for the Day 6 corrections.

These cover the ranking caveat and hypothesis roles, false-positive
classification in the offline evaluation, provenance checks that run before a
report is served, bounded cited evidence on long spans, and structured errors.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import ConfigurationError, Environment, Settings, load_settings
from app.domain.investigation_records import (
    HypothesisKind,
    HypothesisRole,
    Investigation,
)
from app.domain.telemetry import MetricName, ScenarioId, TelemetryEvent
from app.main import create_app
from app.routers import incident_access
from app.services.baseline_trust import TrustedBaselines
from app.services.incident_pipeline import ObservedRun, build_incidents, score_run
from app.services.investigation import (
    MAX_CITED_EVENT_IDS,
    RANKING_CAVEAT,
    investigate,
)
from app.services.investigation_evaluation import (
    evaluate_investigations,
    labelled_window,
)
from app.services.investigation_provenance import provenance_violations
from app.services.offline_validation import approve_baseline_specs
from app.services.simulation import SimulationSpec, run_simulation
from tests.incident_factories import (
    BASELINE_SEEDS,
    EVALUATION_SEED,
    TEST_TICKS,
    baseline_runs,
    observe,
    simulate,
    trusted_baselines,
)

INCIDENTS = "/api/v1/incidents"
NORMAL = ScenarioId.NORMAL_OPERATION
FALSE_POSITIVE_SEED = 606
EVAL_BASELINE_SEEDS = (101, 102, 103, 104)
EVAL_TICKS = 60


def _report(
    scenario: ScenarioId,
    seed: int = EVALUATION_SEED,
    *,
    max_cited_event_ids: int = MAX_CITED_EVENT_IDS,
) -> tuple[ObservedRun, Investigation]:
    """Build one real report at the test tick count, with its source run."""
    baselines = baseline_runs()
    run = simulate(scenario, seed)
    observed = [observe(item) for item in [*baselines, run]]
    baseline_ids = [item.run_id for item in baselines]
    trusted = trusted_baselines()
    incident = next(
        item
        for item in build_incidents(observed, baseline_ids, trusted=trusted)
        if item.source_run_id == run.run_id
    )
    verdicts = score_run(observed, baseline_ids, run.run_id, trusted=trusted)
    report = investigate(
        incident,
        verdicts,
        max_cited_event_ids=max_cited_event_ids,
    )
    source = next(item for item in observed if item.run_id == run.run_id)
    return source, report


def _events_by_id(source: ObservedRun) -> dict[str, TelemetryEvent]:
    """Index the source run's stored events by ID, as the API does."""
    return {event.event_id: event for event in source.events}


# --- Ranking caveat and roles -------------------------------------------------


@pytest.mark.parametrize("scenario", [s for s in ScenarioId if s is not NORMAL])
def test_every_report_carries_the_ranking_caveat(scenario: ScenarioId) -> None:
    _, report = _report(scenario)

    assert report.ranking_caveat.startswith(RANKING_CAVEAT)
    assert "not a measure of causal certainty" in report.ranking_caveat
    assert all(item.verification_status == "unverified" for item in report.hypotheses)


def test_api_symptom_at_rank_one_is_named_as_downstream_in_the_caveat() -> None:
    _, report = _report(ScenarioId.CONNECTION_FAILURE)

    top = report.hypotheses[0]
    assert top.kind is HypothesisKind.API_ERROR_PROPAGATION
    assert top.role is HypothesisRole.DOWNSTREAM_SYMPTOM
    assert "Rank 1, api_error_propagation, is a downstream symptom" in (
        report.ranking_caveat
    )
    assert "database_connectivity_problem" in report.ranking_caveat


def test_competing_hypotheses_are_kept_when_a_symptom_ranks_first() -> None:
    _, report = _report(ScenarioId.CONNECTION_FAILURE)

    kinds = {item.kind for item in report.hypotheses}
    assert HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM in kinds
    initiating = next(
        item
        for item in report.hypotheses
        if item.kind is HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM
    )
    assert initiating.role is HypothesisRole.POTENTIAL_INITIATING_FAULT
    assert initiating.kind in report.hypotheses[0].alternative_kinds


def test_cpu_saturation_rank_one_is_not_labelled_a_symptom() -> None:
    _, report = _report(ScenarioId.CPU_SATURATION)

    assert report.hypotheses[0].role is HypothesisRole.POTENTIAL_INITIATING_FAULT
    assert "is a downstream symptom" not in report.ranking_caveat


# --- False-positive classification -------------------------------------------


def test_extra_incident_outside_the_window_is_a_false_positive() -> None:
    baselines = [
        run_simulation(
            SimulationSpec(scenario=NORMAL, seed=seed, ticks=EVAL_TICKS)
        )
        for seed in EVAL_BASELINE_SEEDS
    ]
    run = run_simulation(
        SimulationSpec(
            scenario=ScenarioId.CONNECTION_FAILURE,
            seed=FALSE_POSITIVE_SEED,
            ticks=EVAL_TICKS,
        )
    )

    (score,) = evaluate_investigations(baselines, [run])

    assert score.incident_count == 2
    assert score.window_incidents == 1
    assert score.false_positive_incidents == 1
    assert score.incident_count == (
        score.window_incidents + score.false_positive_incidents
    )


def test_normal_run_has_no_labelled_window() -> None:
    run = simulate(NORMAL, 311, ticks=EVAL_TICKS)

    assert labelled_window(run.events) is None


def test_labelled_window_spans_the_anomalous_ticks() -> None:
    run = simulate(ScenarioId.CONNECTION_FAILURE, 311, ticks=EVAL_TICKS)
    times = sorted(event.timestamp for event in run.events if event.label.is_anomalous)

    assert labelled_window(run.events) == (times[0], times[-1])


# --- Provenance ---------------------------------------------------------------


def test_a_valid_report_has_no_provenance_violations() -> None:
    source, report = _report(ScenarioId.CPU_SATURATION)

    assert provenance_violations(report, _events_by_id(source)) == []


def test_unknown_event_is_reported() -> None:
    source, report = _report(ScenarioId.CPU_SATURATION)
    events = _events_by_id(source)
    cited = report.verified_observations[0].source_event_ids[0]
    del events[cited]

    violations = provenance_violations(report, events)

    assert any(f"unknown event {cited}" in item for item in violations)


def test_event_from_another_run_is_reported() -> None:
    source, report = _report(ScenarioId.CPU_SATURATION)
    events = _events_by_id(source)
    cited = report.verified_observations[0].source_event_ids[0]
    events[cited] = events[cited].model_copy(
        update={"run_id": "sim-0000000000000000"}
    )

    violations = provenance_violations(report, events)

    assert f"{cited} belongs to another run" in violations


def test_event_on_the_wrong_series_is_reported() -> None:
    source, report = _report(ScenarioId.CPU_SATURATION)
    events = _events_by_id(source)
    cited = report.verified_observations[0].source_event_ids[0]
    original = events[cited]
    other = next(metric for metric in MetricName if metric is not original.metric_name)
    events[cited] = original.model_copy(update={"metric_name": other})

    violations = provenance_violations(report, events)

    assert any(
        item.startswith(f"{cited} is not on a series") for item in violations
    )


def test_event_outside_the_span_is_reported() -> None:
    source, report = _report(ScenarioId.CPU_SATURATION)
    events = _events_by_id(source)
    cited = report.verified_observations[0].source_event_ids[0]
    original = events[cited]
    events[cited] = original.model_copy(
        update={"timestamp": original.timestamp + timedelta(days=1)}
    )

    violations = provenance_violations(report, events)

    assert f"{cited} lies outside its observation or the span" in violations


def test_hypothesis_citing_an_event_no_observation_lists_is_reported() -> None:
    source, report = _report(ScenarioId.CPU_SATURATION)
    hypothesis = report.hypotheses[0]
    listed = set().union(
        *(
            item.source_event_ids
            for item in report.verified_observations
            if item.observation_id in hypothesis.supporting_observation_ids
        )
    )
    stray = next(
        event.event_id for event in source.events if event.event_id not in listed
    )
    tampered = hypothesis.model_copy(
        update={"supporting_event_ids": (*hypothesis.supporting_event_ids, stray)}
    )
    forged = report.model_copy(
        update={"hypotheses": (tampered, *report.hypotheses[1:])}
    )

    violations = provenance_violations(forged, _events_by_id(source))

    assert any(f"cites {stray}" in item for item in violations)


# --- Bounded evidence on long spans -------------------------------------------


def test_cap_limits_listed_events_and_keeps_the_totals() -> None:
    _, full = _report(ScenarioId.CPU_SATURATION)
    _, capped = _report(ScenarioId.CPU_SATURATION, max_cited_event_ids=3)

    full_totals = {
        item.observation_id: item.source_event_count
        for item in full.verified_observations
    }
    for observation in capped.verified_observations:
        assert len(observation.source_event_ids) <= 3
        assert observation.source_event_count == full_totals[observation.observation_id]
        assert observation.source_events_truncated == (
            observation.source_event_count > len(observation.source_event_ids)
        )
    assert capped.evidence_truncated is True


def test_cap_does_not_change_ranking_or_statements() -> None:
    _, full = _report(ScenarioId.CPU_SATURATION)
    _, capped = _report(ScenarioId.CPU_SATURATION, max_cited_event_ids=3)

    assert [item.kind for item in capped.hypotheses] == [
        item.kind for item in full.hypotheses
    ]
    assert [item.statement for item in capped.verified_observations] == [
        item.statement for item in full.verified_observations
    ]
    assert capped.ranking_caveat == full.ranking_caveat


def test_capped_report_is_still_fully_provenanced() -> None:
    source, capped = _report(ScenarioId.CPU_SATURATION, max_cited_event_ids=3)

    assert provenance_violations(capped, _events_by_id(source)) == []
    assert Investigation.model_validate(capped.model_dump(mode="json")) == capped


def test_capped_hypothesis_lists_only_events_its_observations_show() -> None:
    _, capped = _report(ScenarioId.CPU_SATURATION, max_cited_event_ids=3)

    for hypothesis in capped.hypotheses:
        shown = set().union(
            *(
                item.source_event_ids
                for item in capped.verified_observations
                if item.observation_id in hypothesis.supporting_observation_ids
            )
        )
        assert set(hypothesis.supporting_event_ids) <= shown
        assert len(hypothesis.supporting_event_ids) <= 3


def test_cap_must_be_positive() -> None:
    baselines = baseline_runs()
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    observed = [observe(item) for item in [*baselines, run]]
    baseline_ids = [item.run_id for item in baselines]
    trusted = trusted_baselines()
    incident = next(
        item
        for item in build_incidents(observed, baseline_ids, trusted=trusted)
        if item.source_run_id == run.run_id
    )
    verdicts = score_run(observed, baseline_ids, run.run_id, trusted=trusted)

    with pytest.raises(ValueError, match="at least 1"):
        investigate(incident, verdicts, max_cited_event_ids=0)


def test_cap_is_read_from_the_environment_and_validated() -> None:
    settings = load_settings({"AEGIS_INVESTIGATION_MAX_CITED_EVENTS": "5"})

    assert settings.investigation_max_cited_events == 5
    assert load_settings({}).investigation_max_cited_events == 100
    with pytest.raises(ConfigurationError):
        load_settings({"AEGIS_INVESTIGATION_MAX_CITED_EVENTS": "0"})


# --- Structured errors --------------------------------------------------------


def test_duplicate_event_ids_are_refused_as_an_analysis_failure() -> None:
    baselines = baseline_runs()
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    duplicated = replace(
        observe(run),
        events=(*run.events, run.events[0]),
    )
    observed = [observe(item) for item in baselines] + [duplicated]
    baseline_ids = [item.run_id for item in baselines]

    with pytest.raises(HTTPException) as failure:
        incident_access.incidents_for(
            observed,
            trusted_baselines(),
            baseline_ids,
        )

    assert failure.value.status_code == 500
    assert failure.value.detail["code"] == "analysis_failed"
    assert "repeats an event ID" not in str(failure.value.detail)


def test_empty_alert_group_is_refused_by_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.incident_pipeline.group_alerts",
        lambda *_args, **_kwargs: [[]],
    )
    baselines = baseline_runs()
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    observed = [observe(item) for item in [*baselines, run]]

    with pytest.raises(ValueError, match="alert group is empty"):
        build_incidents(
            observed,
            [item.run_id for item in baselines],
            trusted=trusted_baselines(),
        )


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An application with the test baselines approved and simulation enabled."""
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
    )
    application = create_app(settings, trusted_baselines=trusted_baselines())
    with TestClient(application) as test_client:
        yield test_client


def _seed(client: TestClient, scenario: str) -> dict[str, str]:
    """Store the baselines and one fault run through the API; return their IDs."""
    run_ids: dict[str, str] = {}
    for seed in BASELINE_SEEDS:
        run_ids[f"baseline-{seed}"] = _post(client, "normal_operation", seed)
    run_ids["fault"] = _post(client, scenario, EVALUATION_SEED)
    return run_ids


def _post(client: TestClient, scenario: str, seed: int) -> str:
    """Create one simulation through the API and return its run ID."""
    response = client.post(
        "/api/v1/simulations",
        json={"scenario": scenario, "seed": seed, "ticks": TEST_TICKS},
    )
    assert response.status_code in (200, 201)
    run_id: str = response.json()["run_id"]
    return run_id


def _query(run_ids: dict[str, str]) -> list[tuple[str, str]]:
    """Return the repeated baseline parameter for the seeded baselines."""
    return [
        ("baseline_run_id", run_ids[f"baseline-{seed}"]) for seed in BASELINE_SEEDS
    ]


def _first_incident(client: TestClient, run_ids: dict[str, str]) -> str:
    """Return the first incident the list endpoint reports for the fault run."""
    response = client.get(
        INCIDENTS,
        params=[*_query(run_ids), ("run_id", run_ids["fault"])],
    )
    assert response.status_code == 200
    incident_id: str = response.json()["items"][0]["incident_id"]
    return incident_id


def test_provenance_violation_returns_a_generic_500(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_ids = _seed(client, "cpu_saturation")
    incident_id = _first_incident(client, run_ids)
    monkeypatch.setattr(
        "app.routers.investigations.provenance_violations",
        lambda _report, _events: ["forced violation for the test"],
    )

    with caplog.at_level("ERROR"):
        response = client.get(
            f"{INCIDENTS}/{incident_id}/investigation",
            params=_query(run_ids),
        )

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "analysis_failed",
        "message": "the incident analysis could not be completed",
    }
    assert "forced violation" not in response.text
    assert "failed provenance" in caplog.text


def test_empty_alert_group_through_the_api_is_a_generic_500(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = _seed(client, "cpu_saturation")
    monkeypatch.setattr(
        "app.services.incident_pipeline.group_alerts",
        lambda *_args, **_kwargs: [[]],
    )

    response = client.get(INCIDENTS, params=[*_query(run_ids)])

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "analysis_failed"
    assert "alert group" not in response.text


def test_configured_cap_applies_through_the_api() -> None:
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
        investigation_max_cited_events=2,
    )
    application = create_app(settings, trusted_baselines=trusted_baselines())
    with TestClient(application) as client:
        run_ids = _seed(client, "cpu_saturation")
        incident_id = _first_incident(client, run_ids)
        response = client.get(
            f"{INCIDENTS}/{incident_id}/investigation",
            params=_query(run_ids),
        )

    assert response.status_code == 200
    report = Investigation.model_validate(response.json())
    assert all(len(item.source_event_ids) <= 2 for item in report.verified_observations)
    assert report.evidence_truncated is True
