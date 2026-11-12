"""Tests for the read-only investigation endpoint: trust, errors, and read-only use."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.domain.investigation_records import HypothesisKind, Investigation
from app.domain.telemetry import ScenarioId
from app.main import create_app
from app.services.simulation import SimulationSpec, run_id_for
from tests.incident_factories import (
    BASELINE_SEEDS,
    EVALUATION_SEED,
    TEST_TICKS,
    trusted_baselines,
)

INCIDENTS = "/api/v1/incidents"
EVENTS = "/api/v1/events"
MISSING_INCIDENT = "inc-0000000000000000"
APPROVED_UNSTORED_RUN = run_id_for(
    SimulationSpec(
        scenario=ScenarioId.NORMAL_OPERATION,
        seed=BASELINE_SEEDS[0],
        ticks=TEST_TICKS,
    )
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An application whose trusted baselines are the two normal test seeds only."""
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
    )
    application = create_app(settings, trusted_baselines=trusted_baselines())
    with TestClient(application) as test_client:
        yield test_client


def _post_run(client: TestClient, scenario: str, seed: int) -> str:
    """Create a simulation through the API and return its run ID."""
    response = client.post(
        "/api/v1/simulations",
        json={"scenario": scenario, "seed": seed, "ticks": 288},
    )
    assert response.status_code in (200, 201)
    run_id: str = response.json()["run_id"]
    return run_id


def _seed_store(client: TestClient, scenario: str = "cpu_saturation") -> dict[str, str]:
    """Store the two baselines and one fault run; return the IDs by role."""
    run_ids = {
        f"baseline-{seed}": _post_run(client, "normal_operation", seed)
        for seed in BASELINE_SEEDS
    }
    run_ids["fault"] = _post_run(client, scenario, EVALUATION_SEED)
    return run_ids


def _baseline_query(run_ids: dict[str, str]) -> list[tuple[str, str]]:
    """Return the repeated ``baseline_run_id`` parameter for the seeded baselines."""
    return [
        ("baseline_run_id", run_ids[f"baseline-{seed}"]) for seed in BASELINE_SEEDS
    ]


def _first_incident_id(client: TestClient, run_ids: dict[str, str]) -> str:
    """Return the first incident the list endpoint reports for the fault run."""
    response = client.get(
        INCIDENTS,
        params=[*_baseline_query(run_ids), ("run_id", run_ids["fault"])],
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    incident_id: str = items[0]["incident_id"]
    return incident_id


def test_investigation_for_a_fault_run_is_returned_and_valid(
    client: TestClient,
) -> None:
    run_ids = _seed_store(client)
    incident_id = _first_incident_id(client, run_ids)

    response = client.get(
        f"{INCIDENTS}/{incident_id}/investigation",
        params=_baseline_query(run_ids),
    )

    assert response.status_code == 200
    report = Investigation.model_validate(response.json())
    assert report.source_incident_id == incident_id
    assert report.source_run_id == run_ids["fault"]
    assert HypothesisKind.CPU_RESOURCE_SATURATION in {
        hypothesis.kind for hypothesis in report.hypotheses
    }


def test_investigation_is_identical_across_repeated_requests(
    client: TestClient,
) -> None:
    run_ids = _seed_store(client)
    incident_id = _first_incident_id(client, run_ids)
    url = f"{INCIDENTS}/{incident_id}/investigation"

    first = client.get(url, params=_baseline_query(run_ids))
    second = client.get(url, params=_baseline_query(run_ids))

    assert first.json() == second.json()


def test_unapproved_baseline_is_refused_before_analysis(client: TestClient) -> None:
    run_ids = _seed_store(client)
    incident_id = _first_incident_id(client, run_ids)
    fault = run_ids["fault"]

    response = client.get(
        f"{INCIDENTS}/{incident_id}/investigation",
        params=[("baseline_run_id", fault)],
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "baseline_not_approved"


def test_approved_but_unstored_baseline_is_reported_as_not_found() -> None:
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
    )
    with TestClient(
        create_app(settings, trusted_baselines=trusted_baselines())
    ) as client:
        response = client.get(
            f"{INCIDENTS}/{MISSING_INCIDENT}/investigation",
            params=[("baseline_run_id", APPROVED_UNSTORED_RUN)],
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "baseline_run_not_found"


def test_unknown_incident_is_not_found(client: TestClient) -> None:
    run_ids = _seed_store(client)

    response = client.get(
        f"{INCIDENTS}/{MISSING_INCIDENT}/investigation",
        params=_baseline_query(run_ids),
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "incident_not_found"


def test_normal_run_yields_no_incident_and_so_no_investigation(
    client: TestClient,
) -> None:
    run_ids = _seed_store(client, scenario="normal_operation")

    listing = client.get(
        INCIDENTS,
        params=[*_baseline_query(run_ids), ("run_id", run_ids["fault"])],
    )
    response = client.get(
        f"{INCIDENTS}/{MISSING_INCIDENT}/investigation",
        params=_baseline_query(run_ids),
    )

    assert listing.json()["total"] == 0
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "incident_not_found"


@pytest.mark.parametrize("bad_id", ["inc-XYZ", "inc-0000"])
def test_malformed_incident_id_is_rejected_by_validation(
    client: TestClient,
    bad_id: str,
) -> None:
    run_ids = _seed_store(client)

    response = client.get(
        f"{INCIDENTS}/{bad_id}/investigation",
        params=_baseline_query(run_ids),
    )

    assert response.status_code == 422


def test_baseline_run_is_required(client: TestClient) -> None:
    response = client.get(f"{INCIDENTS}/{MISSING_INCIDENT}/investigation")

    assert response.status_code == 422


def test_too_many_baseline_runs_are_rejected(client: TestClient) -> None:
    response = client.get(
        f"{INCIDENTS}/{MISSING_INCIDENT}/investigation",
        params=[("baseline_run_id", f"sim-{i:016x}") for i in range(9)],
    )

    assert response.status_code == 422


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_investigation_endpoint_accepts_only_get(
    client: TestClient,
    method: str,
) -> None:
    run_ids = _seed_store(client)
    incident_id = _first_incident_id(client, run_ids)

    response = client.request(
        method.upper(),
        f"{INCIDENTS}/{incident_id}/investigation",
        params=_baseline_query(run_ids),
    )

    assert response.status_code == 405


def test_investigation_requests_leave_the_store_unchanged(client: TestClient) -> None:
    run_ids = _seed_store(client)
    incident_id = _first_incident_id(client, run_ids)
    before = client.get(EVENTS, params={"limit": 500}).json()

    for _ in range(2):
        client.get(
            f"{INCIDENTS}/{incident_id}/investigation",
            params=_baseline_query(run_ids),
        )

    assert client.get(EVENTS, params={"limit": 500}).json() == before


def test_error_bodies_carry_only_a_code_and_a_message(client: TestClient) -> None:
    run_ids = _seed_store(client)

    response = client.get(
        f"{INCIDENTS}/{MISSING_INCIDENT}/investigation",
        params=_baseline_query(run_ids),
    )

    detail = response.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert "Traceback" not in response.text
    assert "\\" not in response.text
