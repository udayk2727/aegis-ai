"""Tests for the offline, label-aware synthetic baseline validation."""

import pytest

from app.domain.telemetry import ScenarioId
from app.services.offline_validation import (
    BaselineRejected,
    approve_baseline_specs,
    validate_synthetic_baseline,
)
from app.services.simulation import SimulationSpec, run_simulation
from tests.incident_factories import TEST_TICKS, simulate


def test_a_fault_free_run_is_approved_with_its_provenance() -> None:
    run = simulate(ScenarioId.NORMAL_OPERATION, 301)

    approval = validate_synthetic_baseline(run)

    assert approval.run_id == run.run_id
    assert approval.spec == run.spec
    assert approval.method == "synthetic_ground_truth"
    assert approval.checked_event_count == len(run.events)


@pytest.mark.parametrize(
    "scenario",
    [s for s in ScenarioId if s is not ScenarioId.NORMAL_OPERATION],
)
def test_every_fault_scenario_is_rejected(scenario: ScenarioId) -> None:
    run = simulate(scenario, 301)

    with pytest.raises(BaselineRejected, match="fault scenario"):
        validate_synthetic_baseline(run)


def test_the_label_check_catches_a_faulty_run_whose_scenario_says_normal() -> None:
    # A run relabelled as normal keeps its injected anomalous events. The
    # scenario check cannot see that, so the label check must reject it.
    faulty = simulate(ScenarioId.API_ERRORS, 301)
    relabelled_spec = faulty.spec.model_copy(
        update={"scenario": ScenarioId.NORMAL_OPERATION}
    )
    relabelled = faulty.model_copy(update={"spec": relabelled_spec})
    assert relabelled.anomalous_event_count > 0

    with pytest.raises(BaselineRejected, match="injected anomalies"):
        validate_synthetic_baseline(relabelled)


def test_approving_specs_stops_at_the_first_faulty_candidate() -> None:
    specs = [
        SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=301, ticks=TEST_TICKS),
        SimulationSpec(scenario=ScenarioId.CPU_SATURATION, seed=302, ticks=TEST_TICKS),
    ]

    with pytest.raises(BaselineRejected):
        approve_baseline_specs(specs)


def test_approval_is_deterministic_for_the_same_spec() -> None:
    spec = SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=301, ticks=60)

    first = validate_synthetic_baseline(run_simulation(spec))
    second = validate_synthetic_baseline(run_simulation(spec))

    assert first == second
