"""Tests for the demo listing of approved baselines and the dashboard workflow."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.domain.incident_records import Incident
from app.domain.investigation_records import Investigation
from app.main import create_app
from tests.incident_factories import (
    BASELINE_SEEDS,
    EVALUATION_SEED,
    trusted_baselines,
)

DEMO = "/api/v1/demo/baselines"
INCIDENTS = "/api/v1/incidents"
EVENTS = "/api/v1/events"
MISSING_INCIDENT = "inc-0000000000000000"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An application with the demo API on and the two test baselines approved."""
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
        demo_api_enabled=True,
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


def _baseline_query(run_ids: list[str]) -> list[tuple[str, str]]:
    """Return the repeated ``baseline_run_id`` parameter for the given runs."""
    return [("baseline_run_id", run_id) for run_id in run_ids]


def test_empty_store_lists_no_baselines(client: TestClient) -> None:
    response = client.get(DEMO)

    assert response.status_code == 200
    assert response.json() == {"data_origin": "synthetic", "items": [], "total": 0}


def test_only_approved_stored_baselines_are_listed(client: TestClient) -> None:
    baseline_ids = sorted(
        _post_run(client, "normal_operation", seed) for seed in BASELINE_SEEDS
    )
    _post_run(client, "cpu_saturation", EVALUATION_SEED)

    body = client.get(DEMO).json()

    assert body["data_origin"] == "synthetic"
    assert body["total"] == len(BASELINE_SEEDS) == len(body["items"])
    assert [item["run_id"] for item in body["items"]] == baseline_ids
    for item in body["items"]:
        assert item["scenario_id"] == "normal_operation"
        assert item["ticks"] == 288
        assert item["event_count"] > 0
        assert item["approval_method"]


def test_approved_baseline_that_is_not_stored_is_left_out() -> None:
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
        demo_api_enabled=True,
    )
    application = create_app(settings, trusted_baselines=trusted_baselines())
    with TestClient(application) as test_client:
        stored_id = _post_run(test_client, "normal_operation", BASELINE_SEEDS[0])

        items = test_client.get(DEMO).json()["items"]

    assert [item["run_id"] for item in items] == [stored_id]


def test_demo_route_is_absent_when_the_flag_is_off() -> None:
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.get(DEMO)

    assert response.status_code == 404


def test_demo_route_is_documented_with_a_response_model(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    assert "/api/v1/demo/baselines" in spec["paths"]
    assert set(spec["paths"]["/api/v1/demo/baselines"]) == {"get"}


def test_listing_leaves_the_store_and_approvals_unchanged(client: TestClient) -> None:
    _post_run(client, "normal_operation", BASELINE_SEEDS[0])
    before = client.get(EVENTS, params={"limit": 1}).json()["total"]

    client.get(DEMO)
    client.get(DEMO)

    assert client.get(EVENTS, params={"limit": 1}).json()["total"] == before


def test_demo_workflow_from_listing_to_investigation(client: TestClient) -> None:
    """Prepare data, list baselines, list incidents, then open one investigation."""
    for seed in BASELINE_SEEDS:
        _post_run(client, "normal_operation", seed)
    _post_run(client, "cpu_saturation", EVALUATION_SEED)

    baseline_ids = [item["run_id"] for item in client.get(DEMO).json()["items"]]
    query = _baseline_query(baseline_ids)

    page = client.get(INCIDENTS, params=query).json()
    assert page["total"] >= 1
    incident_id = page["items"][0]["incident_id"]

    incident = client.get(f"{INCIDENTS}/{incident_id}", params=query)
    assert incident.status_code == 200
    Incident.model_validate(incident.json())

    response = client.get(f"{INCIDENTS}/{incident_id}/investigation", params=query)

    assert response.status_code == 200
    report = Investigation.model_validate(response.json())
    assert report.root_cause_assessment == "not_assessed"
    assert report.hypotheses
    for hypothesis in report.hypotheses:
        assert hypothesis.verification_status == "unverified"
        assert hypothesis.statement


def test_unknown_incident_after_listing_is_a_safe_404(client: TestClient) -> None:
    for seed in BASELINE_SEEDS:
        _post_run(client, "normal_operation", seed)
    query = _baseline_query(
        [item["run_id"] for item in client.get(DEMO).json()["items"]]
    )

    response = client.get(f"{INCIDENTS}/{MISSING_INCIDENT}/investigation", params=query)

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "incident_not_found",
        "message": "no incident with that ID for the given baselines",
    }
