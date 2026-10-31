"""Tests for GET /api/v1/events: retrieval, filtering, pagination, and validation."""

from typing import Any

from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.main import create_app
from app.services.simulation import EVENTS_PER_TICK

DEVELOPMENT_SETTINGS = Settings(
    environment=Environment.DEVELOPMENT,
    simulation_api_enabled=True,
)


def _bounded_client(max_events: int) -> TestClient:
    """Return a client whose application holds at most ``max_events`` events."""
    return TestClient(
        create_app(DEVELOPMENT_SETTINGS, event_capacity=max_events),
    )


def _stored_total(client: TestClient, run_id: str) -> int:
    """Return how many events of one run are currently visible through the API."""
    body: dict[str, Any] = client.get(
        "/api/v1/events", params={"run_id": run_id}
    ).json()
    total: int = body["total"]
    return total


def _create_run(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """Create a run through the public API and return its summary."""
    payload: dict[str, Any] = {
        "scenario": "api_errors",
        "seed": 11,
        "ticks": 12,
    }
    payload.update(overrides)
    response = client.post("/api/v1/simulations", json=payload)
    assert response.status_code in (200, 201)
    body: dict[str, Any] = response.json()
    return body


def test_empty_store_returns_empty_page(client: TestClient) -> None:
    response = client.get("/api/v1/events")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_returns_first_page_by_default(client: TestClient) -> None:
    run = _create_run(client)

    body = client.get("/api/v1/events").json()

    assert body["total"] == run["event_count"]
    assert body["limit"] == 100
    assert len(body["items"]) == 100


def test_event_shape_matches_contract(client: TestClient) -> None:
    _create_run(client)

    event = client.get("/api/v1/events", params={"limit": 1}).json()["items"][0]

    assert set(event) == {
        "event_id",
        "run_id",
        "timestamp",
        "service",
        "event_type",
        "metric_name",
        "value",
        "unit",
        "severity",
        "label",
    }
    assert event["timestamp"].endswith("Z")
    assert event["label"].keys() == {"scenario_id", "is_anomalous"}
    assert event["severity"] in {"info", "warning", "critical"}


def test_filter_by_run_id(client: TestClient) -> None:
    first = _create_run(client, seed=1)
    _create_run(client, seed=2)

    body = client.get("/api/v1/events", params={"run_id": first["run_id"]}).json()

    assert body["total"] == first["event_count"]
    assert {item["run_id"] for item in body["items"]} == {first["run_id"]}


def test_filter_by_anomaly_label_matches_run_summary(client: TestClient) -> None:
    run = _create_run(client)

    anomalous = client.get("/api/v1/events", params={"is_anomalous": "true"}).json()
    normal = client.get("/api/v1/events", params={"is_anomalous": "false"}).json()

    assert anomalous["total"] == run["anomalous_event_count"]
    assert normal["total"] == run["event_count"] - run["anomalous_event_count"]
    assert all(item["label"]["is_anomalous"] for item in anomalous["items"])


def test_filter_by_scenario_service_and_metric(client: TestClient) -> None:
    _create_run(client, scenario="api_errors")
    _create_run(client, scenario="normal_operation", seed=5)

    by_scenario = client.get(
        "/api/v1/events", params={"scenario_id": "api_errors", "limit": 500}
    ).json()
    by_metric = client.get(
        "/api/v1/events",
        params={"metric_name": "http_error_count", "limit": 500},
    ).json()
    by_service = client.get(
        "/api/v1/events", params={"service": "orders-db", "limit": 500}
    ).json()

    assert by_scenario["total"] == 12 * EVENTS_PER_TICK
    scenarios = {item["label"]["scenario_id"] for item in by_scenario["items"]}
    assert scenarios == {"api_errors"}
    assert {item["metric_name"] for item in by_metric["items"]} == {"http_error_count"}
    assert by_metric["total"] == 12 * 2 * 2
    assert {item["service"] for item in by_service["items"]} == {"orders-db"}


def test_filters_combine_with_and(client: TestClient) -> None:
    _create_run(client)

    body = client.get(
        "/api/v1/events",
        params={"service": "api-gateway", "metric_name": "db_query_latency_ms"},
    ).json()

    assert body["total"] == 0


def test_pagination_walks_all_events_without_overlap(client: TestClient) -> None:
    run = _create_run(client, ticks=10)
    seen: list[str] = []
    offset = 0
    while True:
        page = client.get(
            "/api/v1/events", params={"limit": 25, "offset": offset}
        ).json()
        if not page["items"]:
            break
        seen.extend(item["event_id"] for item in page["items"])
        offset += 25

    assert len(seen) == run["event_count"]
    assert len(set(seen)) == run["event_count"]


def test_offset_past_end_returns_no_items_but_keeps_total(client: TestClient) -> None:
    run = _create_run(client)

    body = client.get("/api/v1/events", params={"offset": 100_000}).json()

    assert body["items"] == []
    assert body["total"] == run["event_count"]


def test_limit_above_maximum_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"limit": 501})

    assert response.status_code == 422


def test_limit_below_minimum_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"limit": 0})

    assert response.status_code == 422


def test_negative_offset_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"offset": -1})

    assert response.status_code == 422


def test_unknown_scenario_filter_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"scenario_id": "bogus"})

    assert response.status_code == 422


def test_malformed_service_filter_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"service": "Orders DB"})

    assert response.status_code == 422


def test_malformed_run_id_filter_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"run_id": "../../etc"})

    assert response.status_code == 422


def test_non_boolean_anomaly_filter_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/events", params={"is_anomalous": "maybe"})

    assert response.status_code == 422


def test_each_application_has_its_own_store(client: TestClient) -> None:
    _create_run(client)

    with TestClient(create_app(DEVELOPMENT_SETTINGS)) as other:
        assert other.get("/api/v1/events").json()["total"] == 0


def test_eviction_removes_the_oldest_run_whole() -> None:
    capacity = 2 * 12 * EVENTS_PER_TICK
    with _bounded_client(max_events=capacity) as client:
        first = _create_run(client, seed=1, ticks=12)
        second = _create_run(client, seed=2, ticks=12)
        third = _create_run(client, seed=3, ticks=12)

        assert _stored_total(client, first["run_id"]) == 0
        assert _stored_total(client, second["run_id"]) == second["event_count"]
        assert _stored_total(client, third["run_id"]) == third["event_count"]
        everything = client.get("/api/v1/events", params={"limit": 1}).json()
        assert everything["total"] == capacity


def test_eviction_never_leaves_a_partial_run_visible() -> None:
    with _bounded_client(max_events=120) as client:
        large = _create_run(client, seed=1, ticks=12)  # 108 events
        small = _create_run(client, seed=2, ticks=2)  # 18 events; evicts large

        assert _stored_total(client, large["run_id"]) == 0
        assert _stored_total(client, small["run_id"]) == small["event_count"]

        later = _create_run(client, seed=3, ticks=12)  # 108 events; evicts small

        assert _stored_total(client, small["run_id"]) == 0
        page = client.get("/api/v1/events", params={"limit": 500}).json()
        assert {item["run_id"] for item in page["items"]} == {later["run_id"]}
        assert page["total"] == later["event_count"]

        anomalous = client.get(
            "/api/v1/events",
            params={"run_id": later["run_id"], "is_anomalous": "true"},
        ).json()
        assert anomalous["total"] == later["anomalous_event_count"]


def test_evicted_run_is_stored_again_when_resubmitted() -> None:
    payload = {"scenario": "api_errors", "seed": 1, "ticks": 12}
    with _bounded_client(max_events=120) as client:
        original = client.post("/api/v1/simulations", json=payload)
        other = _create_run(client, seed=2, ticks=12)
        assert original.status_code == 201
        assert _stored_total(client, original.json()["run_id"]) == 0

        replay = client.post("/api/v1/simulations", json=payload)

        assert replay.status_code == 201
        assert replay.json()["run_id"] == original.json()["run_id"]
        assert _stored_total(client, replay.json()["run_id"]) == 108
        assert _stored_total(client, other["run_id"]) == 0
