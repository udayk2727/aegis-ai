"""Tests for the read-only incident endpoints: approval, pagination, and errors."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.domain.incident_records import IncidentSummary
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
MISSING_INCIDENT = "inc-0000000000000000"
UNAPPROVED = {"code": "baseline_not_approved"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An application whose trusted baselines are the two normal test seeds only."""
    trusted = trusted_baselines()
    settings = Settings(
        environment=Environment.DEVELOPMENT,
        simulation_api_enabled=True,
    )
    with TestClient(create_app(settings, trusted_baselines=trusted)) as test_client:
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


def _seed_store(client: TestClient) -> dict[str, str]:
    """Store two baselines and one fault run per fault scenario; return their IDs."""
    run_ids = {
        f"baseline-{seed}": _post_run(client, "normal_operation", seed)
        for seed in BASELINE_SEEDS
    }
    for scenario in ("cpu_saturation", "api_errors"):
        run_ids[scenario] = _post_run(client, scenario, EVALUATION_SEED)
    return run_ids


def _baseline_query(run_ids: dict[str, str]) -> list[tuple[str, str]]:
    """Return the repeated ``baseline_run_id`` parameter for the seeded baselines."""
    return [
        ("baseline_run_id", run_ids[f"baseline-{seed}"]) for seed in BASELINE_SEEDS
    ]


def test_list_returns_summaries_for_every_fault_run(client: TestClient) -> None:
    run_ids = _seed_store(client)

    response = client.get(INCIDENTS, params=_baseline_query(run_ids))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(body["items"]) >= 2
    sources = {item["source_run_id"] for item in body["items"]}
    assert {run_ids["cpu_saturation"], run_ids["api_errors"]} <= sources
    for item in body["items"]:
        assert "evidence" not in item
        assert "timeline" not in item
        IncidentSummary.model_validate(item)


def test_pagination_pages_through_the_same_ordered_list(client: TestClient) -> None:
    run_ids = _seed_store(client)
    query = _baseline_query(run_ids)

    everything = client.get(INCIDENTS, params=query).json()
    first = client.get(INCIDENTS, params=[*query, ("limit", 1)]).json()
    second = client.get(INCIDENTS, params=[*query, ("limit", 1), ("offset", 1)]).json()
    beyond = client.get(
        INCIDENTS,
        params=[*query, ("offset", everything["total"])],
    ).json()

    assert first["items"] == everything["items"][:1]
    assert second["items"] == everything["items"][1:2]
    assert beyond["items"] == []
    assert beyond["total"] == everything["total"]


def test_run_filter_keeps_only_that_runs_incidents(client: TestClient) -> None:
    run_ids = _seed_store(client)

    body = client.get(
        INCIDENTS,
        params=[*_baseline_query(run_ids), ("run_id", run_ids["api_errors"])],
    ).json()

    assert body["items"]
    assert {item["source_run_id"] for item in body["items"]} == {
        run_ids["api_errors"]
    }


def test_a_baseline_run_named_as_run_filter_returns_nothing(client: TestClient) -> None:
    run_ids = _seed_store(client)

    body = client.get(
        INCIDENTS,
        params=[
            *_baseline_query(run_ids),
            ("run_id", run_ids[f"baseline-{BASELINE_SEEDS[0]}"]),
        ],
    ).json()

    assert body["items"] == []
    assert body["total"] == 0


def test_detail_returns_evidence_timeline_and_no_root_cause(
    client: TestClient,
) -> None:
    run_ids = _seed_store(client)
    listing = client.get(INCIDENTS, params=_baseline_query(run_ids)).json()
    incident_id = listing["items"][0]["incident_id"]

    response = client.get(f"{INCIDENTS}/{incident_id}", params=_baseline_query(run_ids))

    assert response.status_code == 200
    body = response.json()
    assert body["incident_id"] == incident_id
    assert body["root_cause_assessment"] == "not_assessed"
    assert body["evidence"]
    assert body["timeline"]
    assert "root_cause" not in body


def test_detail_for_an_unknown_incident_is_a_safe_404(client: TestClient) -> None:
    run_ids = _seed_store(client)

    response = client.get(
        f"{INCIDENTS}/{MISSING_INCIDENT}",
        params=_baseline_query(run_ids),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "incident_not_found",
        "message": "no incident with that ID for the given baselines",
    }


def test_an_unapproved_baseline_is_refused_with_a_structured_403(
    client: TestClient,
) -> None:
    _seed_store(client)

    response = client.get(
        INCIDENTS,
        params=[("baseline_run_id", "sim-ffffffffffffffff")],
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == UNAPPROVED["code"]
    assert "Traceback" not in response.text


def test_a_stored_run_that_was_never_approved_is_refused(client: TestClient) -> None:
    unapproved = _post_run(client, "normal_operation", 999)

    response = client.get(INCIDENTS, params=[("baseline_run_id", unapproved)])

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == UNAPPROVED["code"]


def test_a_fault_run_cannot_be_used_as_a_baseline(client: TestClient) -> None:
    fault = _post_run(client, "cpu_saturation", EVALUATION_SEED)

    response = client.get(
        f"{INCIDENTS}/inc-0000000000000000",
        params=[("baseline_run_id", fault)],
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == UNAPPROVED["code"]


def test_an_approved_baseline_that_is_not_stored_is_a_safe_404(
    client: TestClient,
) -> None:
    approved = run_id_for(
        SimulationSpec(
            scenario=ScenarioId.NORMAL_OPERATION,
            seed=BASELINE_SEEDS[0],
            ticks=TEST_TICKS,
        )
    )

    response = client.get(INCIDENTS, params=[("baseline_run_id", approved)])

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "baseline_run_not_found"


def test_empty_store_gives_an_empty_page_for_a_known_baseline(
    client: TestClient,
) -> None:
    baseline = _post_run(client, "normal_operation", BASELINE_SEEDS[0])

    body = client.get(INCIDENTS, params=[("baseline_run_id", baseline)]).json()

    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_baseline_is_required(client: TestClient) -> None:
    response = client.get(INCIDENTS)

    assert response.status_code == 422


def test_malformed_baseline_run_id_is_rejected(client: TestClient) -> None:
    response = client.get(INCIDENTS, params=[("baseline_run_id", "../etc/passwd")])

    assert response.status_code == 422


def test_too_many_baselines_are_rejected(client: TestClient) -> None:
    baseline = _post_run(client, "normal_operation", BASELINE_SEEDS[0])

    response = client.get(INCIDENTS, params=[("baseline_run_id", baseline)] * 9)

    assert response.status_code == 422


def test_invalid_pagination_is_rejected(client: TestClient) -> None:
    baseline = _post_run(client, "normal_operation", BASELINE_SEEDS[0])
    query = [("baseline_run_id", baseline)]

    assert client.get(INCIDENTS, params=[*query, ("limit", 0)]).status_code == 422
    assert client.get(INCIDENTS, params=[*query, ("limit", 201)]).status_code == 422
    assert client.get(INCIDENTS, params=[*query, ("offset", -1)]).status_code == 422


def test_malformed_incident_id_is_rejected(client: TestClient) -> None:
    run_ids = _seed_store(client)

    response = client.get(
        f"{INCIDENTS}/inc-NOT-A-VALID-ID",
        params=_baseline_query(run_ids),
    )

    assert response.status_code == 422


def test_reading_incidents_does_not_change_the_event_store(client: TestClient) -> None:
    run_ids = _seed_store(client)
    before = client.get("/api/v1/events", params={"limit": 1}).json()["total"]

    client.get(INCIDENTS, params=_baseline_query(run_ids))

    after = client.get("/api/v1/events", params={"limit": 1}).json()["total"]
    assert after == before


def test_incident_endpoints_are_documented_as_read_only(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/api/v1/incidents" in schema["paths"]
    assert set(schema["paths"]["/api/v1/incidents"]) == {"get"}
    assert set(schema["paths"]["/api/v1/incidents/{incident_id}"]) == {"get"}
    assert "403" in schema["paths"]["/api/v1/incidents"]["get"]["responses"]
