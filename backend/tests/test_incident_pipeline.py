"""End-to-end tests for the offline incident pipeline on simulated runs.

These run the real detector and incident rule over deterministic simulator
output. They check properties that must hold for any seed in the chosen set,
rather than exact numbers, so a legitimate change to the detector shows up as a
focused failure instead of a wall of mismatches.
"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.incident_records import (
    Incident,
    InclusionReason,
    TimelineKind,
    TimelineMilestone,
)
from app.domain.telemetry import ScenarioId
from app.services.baseline_trust import TrustedBaselines
from app.services.incident_pipeline import ObservedRun, build_incidents
from tests.incident_factories import (
    EVALUATION_SEED,
    FAULT_SCENARIOS,
    baseline_runs,
    observe,
    simulate,
    trusted_baselines,
    with_flipped_labels,
)
from tests.telemetry_factories import TEST_START

# Seeds where pre-onset error samples are common, so context evidence is present.
CONTEXT_SEEDS = range(301, 305)


def _incidents_for(
    *runs_to_score: ObservedRun,
    baselines: list[ObservedRun],
) -> list[Incident]:
    """Build incidents with the given baseline runs and the runs to score."""
    baseline_ids = [run.run_id for run in baselines]
    return build_incidents(
        [*baselines, *runs_to_score],
        baseline_ids,
        trusted=trusted_baselines(),
    )


@pytest.fixture(scope="module")
def baselines() -> list[ObservedRun]:
    """The shared fault-free baseline runs."""
    return [observe(run) for run in baseline_runs()]


def test_normal_operation_produces_no_incidents(baselines: list[ObservedRun]) -> None:
    normal = observe(simulate(ScenarioId.NORMAL_OPERATION, EVALUATION_SEED))

    assert _incidents_for(normal, baselines=baselines) == []


@pytest.mark.parametrize("scenario", FAULT_SCENARIOS, ids=lambda item: item.value)
def test_each_fault_scenario_is_detected_inside_its_fault_window(
    scenario: ScenarioId,
    baselines: list[ObservedRun],
) -> None:
    run = simulate(scenario, EVALUATION_SEED)
    assert run.anomaly_window is not None

    incidents = _incidents_for(observe(run), baselines=baselines)

    assert incidents, f"{scenario} produced no incident"
    for incident in incidents:
        assert run.anomaly_window.start <= incident.first_detection_at
        assert incident.first_detection_at < run.anomaly_window.end


@pytest.mark.parametrize("scenario", FAULT_SCENARIOS, ids=lambda item: item.value)
def test_every_piece_of_evidence_matches_a_stored_event(
    scenario: ScenarioId,
    baselines: list[ObservedRun],
) -> None:
    run = simulate(scenario, EVALUATION_SEED)
    events_by_id = {event.event_id: event for event in run.events}

    incidents = _incidents_for(observe(run), baselines=baselines)

    for incident in incidents:
        observed_times = [record.observed_at for record in incident.evidence]
        assert observed_times == sorted(observed_times)
        for record in incident.evidence:
            source = events_by_id[record.source_event_id]
            assert record.run_id == source.run_id == incident.source_run_id
            assert record.service == source.service
            assert record.metric_name == source.metric_name
            assert record.observed_at == source.timestamp
            assert record.observed_value == source.value
            assert record.severity == source.severity


def test_incidents_are_deterministic(baselines: list[ObservedRun]) -> None:
    run = observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED))

    first = _incidents_for(run, baselines=baselines)
    second = _incidents_for(run, baselines=baselines)

    assert [item.model_dump() for item in first] == [
        item.model_dump() for item in second
    ]


def test_incident_ids_do_not_depend_on_run_order(baselines: list[ObservedRun]) -> None:
    runs = [
        observe(simulate(ScenarioId.API_ERRORS, EVALUATION_SEED)),
        observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)),
    ]

    forward = _incidents_for(*runs, baselines=baselines)
    backward = _incidents_for(*reversed(runs), baselines=baselines)

    assert [item.incident_id for item in forward] == [
        item.incident_id for item in backward
    ]


def test_labels_never_change_an_incident(baselines: list[ObservedRun]) -> None:
    run = simulate(ScenarioId.DATABASE_LATENCY_SPIKE, EVALUATION_SEED)
    flipped = replace(
        observe(run),
        events=with_flipped_labels(run.events),
    )
    flipped_baselines = [
        replace(item, events=with_flipped_labels(item.events)) for item in baselines
    ]

    original = _incidents_for(observe(run), baselines=baselines)
    relabelled = _incidents_for(flipped, baselines=flipped_baselines)

    assert [item.model_dump() for item in original] == [
        item.model_dump() for item in relabelled
    ]


def test_output_contains_no_ground_truth_fields(baselines: list[ObservedRun]) -> None:
    run = observe(simulate(ScenarioId.CONNECTION_FAILURE, EVALUATION_SEED))

    incidents = _incidents_for(run, baselines=baselines)

    encoded = json.dumps([item.model_dump(mode="json") for item in incidents])
    assert "is_anomalous" not in encoded
    assert "scenario_id" not in encoded


def test_independent_fault_runs_are_never_merged(baselines: list[ObservedRun]) -> None:
    first = observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED))
    second = observe(simulate(ScenarioId.API_ERRORS, EVALUATION_SEED))

    incidents = _incidents_for(first, second, baselines=baselines)

    sources = {item.source_run_id for item in incidents}
    assert sources == {first.run_id, second.run_id}
    for incident in incidents:
        assert {record.run_id for record in incident.evidence} == {
            incident.source_run_id
        }


def test_a_baseline_run_is_never_scored(baselines: list[ObservedRun]) -> None:
    fault = observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED))
    baseline_ids = {run.run_id for run in baselines}

    incidents = _incidents_for(fault, baselines=baselines)

    assert all(item.source_run_id not in baseline_ids for item in incidents)


def test_baseline_only_input_gives_an_empty_result(
    baselines: list[ObservedRun],
) -> None:
    assert (
        build_incidents(
            baselines,
            [run.run_id for run in baselines],
            trusted=trusted_baselines(),
        )
        == []
    )


def test_missing_telemetry_is_reported_as_a_gap(baselines: list[ObservedRun]) -> None:
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    dropped_ticks = {100, 101, 102}
    kept = tuple(
        event
        for event in run.events
        if (event.timestamp - TEST_START).total_seconds() // 60 not in dropped_ticks
    )
    observed = ObservedRun(
        run_id=run.run_id,
        tick_interval_seconds=run.spec.tick_interval_seconds,
        events=kept,
    )

    incidents = _incidents_for(observed, baselines=baselines)

    assert len(incidents) == 1
    gaps = incidents[0].telemetry_gaps
    assert len(gaps) == 1
    assert gaps[0].observed_before == TEST_START + timedelta(minutes=99)
    assert gaps[0].observed_after == TEST_START + timedelta(minutes=103)


def test_an_unknown_baseline_is_refused(baselines: list[ObservedRun]) -> None:
    with pytest.raises(ValueError, match="baseline run not found"):
        build_incidents(baselines, ["sim-ffffffffffffffff"], trusted=trusted_baselines())


def test_at_least_one_baseline_is_required(baselines: list[ObservedRun]) -> None:
    with pytest.raises(ValueError, match="at least one baseline"):
        build_incidents(baselines, [], trusted=trusted_baselines())


def test_an_approved_baseline_is_accepted_by_the_pipeline(
    baselines: list[ObservedRun],
) -> None:
    run = observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED))

    incidents = build_incidents(
        [*baselines, run],
        [baseline.run_id for baseline in baselines],
        trusted=trusted_baselines(),
    )

    assert incidents
    assert {incident.source_run_id for incident in incidents} == {run.run_id}


def test_a_fault_run_named_as_baseline_is_refused_by_the_pipeline(
    baselines: list[ObservedRun],
) -> None:
    fault = observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED))

    with pytest.raises(ValueError, match="baseline run is not approved"):
        build_incidents(
            [*baselines, fault],
            [fault.run_id],
            trusted=trusted_baselines(),
        )


def test_a_stored_normal_run_that_was_never_approved_is_refused(
    baselines: list[ObservedRun],
) -> None:
    unapproved = observe(simulate(ScenarioId.NORMAL_OPERATION, 999))

    with pytest.raises(ValueError, match="baseline run is not approved"):
        build_incidents(
            [*baselines, unapproved],
            [unapproved.run_id],
            trusted=trusted_baselines(),
        )


def test_an_empty_registry_trusts_no_baseline(baselines: list[ObservedRun]) -> None:
    with pytest.raises(ValueError, match="baseline run is not approved"):
        build_incidents(
            baselines,
            [run.run_id for run in baselines],
            trusted=TrustedBaselines(),
        )


def test_timeline_separates_observations_from_the_detection_decision(
    baselines: list[ObservedRun],
) -> None:
    run = observe(simulate(ScenarioId.API_ERRORS, EVALUATION_SEED))

    incident = _incidents_for(run, baselines=baselines)[0]

    timestamps = [entry.timestamp for entry in incident.timeline]
    assert timestamps == sorted(timestamps)

    decisions = [
        entry
        for entry in incident.timeline
        if entry.kind is TimelineKind.DETECTION_DECISION
    ]
    assert len(decisions) == 1
    assert decisions[0].milestone is TimelineMilestone.FIRST_QUALIFYING_DETECTION
    assert decisions[0].timestamp == incident.first_detection_at
    assert decisions[0].source_event_id is None

    observations = [
        entry for entry in incident.timeline if entry.kind is TimelineKind.OBSERVATION
    ]
    assert observations
    assert all(entry.source_event_id is not None for entry in observations)


def test_context_observations_never_count_as_supporting_evidence(
    baselines: list[ObservedRun],
) -> None:
    runs = [
        observe(simulate(scenario, seed))
        for seed in CONTEXT_SEEDS
        for scenario in FAULT_SCENARIOS
    ]

    incidents = _incidents_for(*runs, baselines=baselines)

    context_records = 0
    for incident in incidents:
        contributed = {
            record.source_event_id
            for record in incident.evidence
            if record.inclusion_reason is InclusionReason.CONTRIBUTED_TO_ALERT
        }
        context_records += sum(
            1
            for record in incident.evidence
            if record.inclusion_reason is InclusionReason.PRE_INCIDENT_CONTEXT
        )
        assert incident.first_observation_at == incident.first_detection_at
        observations = [
            entry
            for entry in incident.timeline
            if entry.kind is TimelineKind.OBSERVATION
        ]
        assert {entry.source_event_id for entry in observations} <= contributed
    # Guards against the test passing only because no context exists in its seeds.
    assert context_records > 0


def test_a_run_that_repeats_an_event_id_is_refused(
    baselines: list[ObservedRun],
) -> None:
    run = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    observed = ObservedRun(
        run_id=run.run_id,
        tick_interval_seconds=run.spec.tick_interval_seconds,
        events=(*run.events, run.events[0]),
    )

    with pytest.raises(ValueError, match="repeats an event ID"):
        _incidents_for(observed, baselines=baselines)


def test_a_run_is_isolated_from_the_other_runs_scored_with_it(
    baselines: list[ObservedRun],
) -> None:
    first = observe(simulate(ScenarioId.API_ERRORS, EVALUATION_SEED))
    second = observe(simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED + 1))

    alone = _incidents_for(first, baselines=baselines)
    together = [
        incident
        for incident in _incidents_for(first, second, baselines=baselines)
        if incident.source_run_id == first.run_id
    ]

    assert together == alone


def test_evidence_classes_follow_their_position_in_time(
    baselines: list[ObservedRun],
) -> None:
    runs = [
        observe(simulate(scenario, seed))
        for seed in CONTEXT_SEEDS
        for scenario in FAULT_SCENARIOS
    ]

    incidents = _incidents_for(*runs, baselines=baselines)

    classes_seen = set()
    for incident in incidents:
        for record in incident.evidence:
            reason = record.inclusion_reason
            classes_seen.add(reason)
            if reason is InclusionReason.CONTRIBUTED_TO_ALERT:
                assert incident.first_detection_at <= record.observed_at
                assert record.observed_at <= incident.latest_detection_at
            elif reason is InclusionReason.PRE_INCIDENT_CONTEXT:
                assert record.observed_at < incident.first_detection_at
            else:
                assert record.observed_at > incident.first_detection_at
    assert classes_seen == set(InclusionReason)
