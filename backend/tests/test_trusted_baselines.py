"""Tests for explicit baseline trust: approval, manifest loading, and settings."""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Environment, Settings, load_settings
from app.domain.telemetry import ScenarioId
from app.main import create_app
from app.services.baseline_trust import TrustedBaselines
from app.services.offline_validation import BaselineRejected, approve_baseline_specs
from app.services.simulation import EVENTS_PER_TICK, SimulationSpec, run_id_for
from app.services.trusted_baselines import (
    MANIFEST_VERSION,
    TrustedBaselineError,
    load_trusted_baselines,
    manifest_text,
)
from tests.incident_factories import TEST_TICKS

NORMAL = SimulationSpec(
    scenario=ScenarioId.NORMAL_OPERATION,
    seed=301,
    ticks=TEST_TICKS,
)
FAULT = SimulationSpec(
    scenario=ScenarioId.CPU_SATURATION,
    seed=311,
    ticks=TEST_TICKS,
)


def _trusted(specs: Iterable[SimulationSpec]) -> TrustedBaselines:
    """Approve specs through the offline check, as the test suite's trusted path."""
    return TrustedBaselines(tuple(approve_baseline_specs(specs)))


def _write(path: Path, document: object) -> Path:
    """Write a JSON document to a manifest path and return it."""
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _entry(spec: SimulationSpec, run_id: str | None = None) -> dict[str, object]:
    """Build a manifest entry for a spec, with its correct or a supplied run ID."""
    return {
        "run_id": run_id if run_id is not None else run_id_for(spec),
        "spec": spec.model_dump(mode="json"),
    }


def test_trusted_normal_baseline_is_accepted() -> None:
    trusted = _trusted([NORMAL])

    assert trusted.is_approved(run_id_for(NORMAL))
    (approval,) = trusted.approvals
    assert approval.method == "synthetic_ground_truth"
    assert approval.checked_event_count == TEST_TICKS * EVENTS_PER_TICK


def test_unapproved_baseline_is_not_trusted() -> None:
    trusted = _trusted([NORMAL])

    assert not trusted.is_approved("sim-ffffffffffffffff")
    assert not TrustedBaselines().is_approved(run_id_for(NORMAL))


def test_a_fault_spec_cannot_be_approved() -> None:
    with pytest.raises(BaselineRejected, match="fault scenario cpu_saturation"):
        _trusted([FAULT])


def test_manifest_round_trip_restores_the_same_approvals(tmp_path: Path) -> None:
    original = _trusted([NORMAL])
    path = tmp_path / "trusted.json"
    path.write_text(manifest_text(original.approvals), encoding="utf-8")

    loaded = load_trusted_baselines(path)

    assert loaded.run_ids == original.run_ids


def test_manifest_with_a_fault_entry_is_refused_entirely(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trusted.json",
        {
            "version": MANIFEST_VERSION,
            "baselines": [_entry(NORMAL), _entry(FAULT)],
        },
    )

    with pytest.raises(TrustedBaselineError, match="fault scenario"):
        load_trusted_baselines(path)


def test_manifest_run_id_that_does_not_match_its_spec_is_refused(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "trusted.json",
        {
            "version": MANIFEST_VERSION,
            "baselines": [_entry(NORMAL, run_id=run_id_for(FAULT))],
        },
    )

    with pytest.raises(TrustedBaselineError, match="does not match its spec"):
        load_trusted_baselines(path)


@pytest.mark.parametrize(
    "document",
    [
        {"version": 99, "baselines": []},
        {"version": MANIFEST_VERSION},
        {"version": MANIFEST_VERSION, "baselines": ["not an object"]},
        {"version": MANIFEST_VERSION, "baselines": [{"spec": {}}]},
        [],
    ],
)
def test_malformed_manifest_is_refused(tmp_path: Path, document: object) -> None:
    path = _write(tmp_path / "trusted.json", document)

    with pytest.raises(TrustedBaselineError):
        load_trusted_baselines(path)


def test_unreadable_manifest_is_refused_with_a_safe_message(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(TrustedBaselineError) as error:
        load_trusted_baselines(path)

    assert str(error.value) == "trusted baseline manifest cannot be read"


def test_trusted_baseline_path_is_refused_in_production(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="development-only"):
        Settings(
            environment=Environment.PRODUCTION,
            trusted_baselines_path=tmp_path / "trusted.json",
        )


def test_trusted_baseline_path_is_read_from_the_environment(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "AEGIS_ENVIRONMENT": "development",
            "AEGIS_TRUSTED_BASELINES_PATH": str(tmp_path / "trusted.json"),
        }
    )

    assert settings.trusted_baselines_path == tmp_path / "trusted.json"


def test_a_run_stamped_at_a_different_time_has_a_different_id() -> None:
    later = NORMAL.model_copy(
        update={"start_time": datetime(2027, 1, 1, tzinfo=UTC)},
    )

    assert run_id_for(later) != run_id_for(NORMAL)


def test_without_a_manifest_every_baseline_is_refused() -> None:
    settings = Settings(environment=Environment.DEVELOPMENT)

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/incidents",
            params=[("baseline_run_id", run_id_for(NORMAL))],
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "baseline_not_approved"
