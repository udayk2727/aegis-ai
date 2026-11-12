"""Validation tests for the incident data contracts."""

from datetime import timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.incident_records import Incident, IncidentId, TelemetryGap
from app.domain.telemetry import ScenarioId
from app.services.incident_pipeline import build_incidents
from tests.incident_factories import (
    EVALUATION_SEED,
    baseline_runs,
    observe,
    simulate,
    trusted_baselines,
)
from tests.telemetry_factories import TEST_START


@pytest.fixture(scope="module")
def incident() -> Incident:
    """One real incident from a fault run, to validate against."""
    baselines = baseline_runs()
    fault = simulate(ScenarioId.CPU_SATURATION, EVALUATION_SEED)
    incidents = build_incidents(
        [observe(run) for run in [*baselines, fault]],
        [run.run_id for run in baselines],
        trusted=trusted_baselines(),
    )
    assert incidents, "the fault run must produce an incident for these tests"
    return incidents[0]


@pytest.mark.parametrize(
    "candidate",
    [
        "inc-0123456789abcdef",
        "inc-0000000000000000",
    ],
)
def test_incident_id_accepts_the_derived_format(candidate: str) -> None:
    assert TypeAdapter(IncidentId).validate_python(candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "incident-0123456789abcdef",
        "inc-0123456789ABCDEF",
        "inc-0123456789abcde",
        "inc-0123456789abcdef0",
        "inc-../../etc/passwd",
        "",
    ],
)
def test_incident_id_rejects_anything_else(candidate: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(IncidentId).validate_python(candidate)


def test_telemetry_gap_must_run_forward_in_time() -> None:
    with pytest.raises(ValidationError):
        TelemetryGap(
            observed_before=TEST_START + timedelta(minutes=5),
            observed_after=TEST_START,
        )


def test_telemetry_gap_rejects_equal_endpoints() -> None:
    with pytest.raises(ValidationError):
        TelemetryGap(observed_before=TEST_START, observed_after=TEST_START)


def test_incident_rejects_detection_before_its_first_observation(
    incident: Incident,
) -> None:
    data = incident.model_dump()
    data["first_observation_at"] = incident.first_detection_at + timedelta(minutes=1)

    with pytest.raises(ValidationError, match="out of order"):
        Incident.model_validate(data)


def test_incident_rejects_latest_observation_before_latest_detection(
    incident: Incident,
) -> None:
    data = incident.model_dump()
    data["latest_observation_at"] = incident.latest_detection_at - timedelta(minutes=1)

    with pytest.raises(ValidationError, match="out of order"):
        Incident.model_validate(data)


def test_incident_rejects_an_unknown_field(incident: Incident) -> None:
    data = incident.model_dump()
    data["root_cause"] = "database saturation"

    with pytest.raises(ValidationError):
        Incident.model_validate(data)


def test_incident_rejects_a_root_cause_claim_as_status(incident: Incident) -> None:
    data = incident.model_dump()
    data["root_cause_assessment"] = "confirmed"

    with pytest.raises(ValidationError):
        Incident.model_validate(data)


def test_incident_without_evidence_is_rejected(incident: Incident) -> None:
    data = incident.model_dump()
    data["evidence"] = []

    with pytest.raises(ValidationError):
        Incident.model_validate(data)
