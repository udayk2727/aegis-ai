"""Tests for POST /api/v1/simulations: creation, replay, and input validation."""

from typing import Any

from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.main import create_app
from app.services.simulation import EVENTS_PER_TICK

DEVELOPMENT_SETTINGS = Settings(
    environment=Environment.DEVELOPMENT,
    simulation_api_enabled=True,
)

VALID_REQUEST: dict[str, Any] = {
    "scenario": "database_latency_spike",
    "seed": 7,
    "ticks": 30,
}


def test_run_larger_than_the_store_is_refused_with_structured_422() -> None:
    # A 12-tick run is 108 events, so a 100-event store can never hold it.
    with TestClient(
        create_app(DEVELOPMENT_SETTINGS, event_capacity=100)
    ) as small_store:
        response = small_store.post(
            "/api/v1/simulations",
            json={"scenario": "api_errors", "seed": 1, "ticks": 12},
        )

        assert response.status_code == 422
        assert response.json() == {
            "detail": {
                "code": "run_exceeds_capacity",
                "message": (
                    "the simulation run is larger than the event store can hold"
                ),
                "run_event_count": 108,
            }
        }
        assert small_store.get("/api/v1/events").json()["total"] == 0


def test_run_exactly_at_store_capacity_is_accepted() -> None:
    with TestClient(
        create_app(DEVELOPMENT_SETTINGS, event_capacity=108)
    ) as exact_store:
        response = exact_store.post(
            "/api/v1/simulations",
            json={"scenario": "api_errors", "seed": 1, "ticks": 12},
        )

        assert response.status_code == 201
        assert exact_store.get("/api/v1/events").json()["total"] == 108


def test_oversized_run_does_not_evict_stored_runs() -> None:
    with TestClient(
        create_app(DEVELOPMENT_SETTINGS, event_capacity=100)
    ) as small_store:
        kept = small_store.post(
            "/api/v1/simulations",
            json={"scenario": "api_errors", "seed": 1, "ticks": 2},
        )
        refused = small_store.post(
            "/api/v1/simulations",
            json={"scenario": "api_errors", "seed": 2, "ticks": 12},
        )

        assert kept.status_code == 201
        assert refused.status_code == 422
        page = small_store.get("/api/v1/events", params={"limit": 1}).json()
        assert page["total"] == kept.json()["event_count"]


def test_simulation_endpoint_is_not_mounted_in_production() -> None:
    production = Settings(environment=Environment.PRODUCTION)
    with TestClient(create_app(production)) as prod:
        response = prod.post("/api/v1/simulations", json=VALID_REQUEST)

        assert response.status_code == 404
        assert prod.get("/api/v1/events").status_code == 200


def test_create_returns_201_with_summary_and_no_events(client: TestClient) -> None:
    response = client.post("/api/v1/simulations", json=VALID_REQUEST)

    assert response.status_code == 201
    body = response.json()
    assert body["scenario_id"] == "database_latency_spike"
    assert body["seed"] == 7
    assert body["ticks"] == 30
    assert body["tick_interval_seconds"] == 60
    assert body["start_time"] == "2026-01-01T00:00:00Z"
    assert body["event_count"] == 30 * EVENTS_PER_TICK
    assert body["anomalous_event_count"] == 10 * EVENTS_PER_TICK
    assert body["anomaly_window"] == {
        "start": "2026-01-01T00:10:00Z",
        "end": "2026-01-01T00:20:00Z",
    }
    assert body["run_id"].startswith("sim-")
    assert "items" not in body
    assert "events" not in body


def test_normal_operation_has_null_anomaly_window(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={"scenario": "normal_operation", "seed": 1, "ticks": 5},
    )

    assert response.status_code == 201
    assert response.json()["anomaly_window"] is None
    assert response.json()["anomalous_event_count"] == 0


def test_identical_request_replays_with_200_and_same_body(client: TestClient) -> None:
    first = client.post("/api/v1/simulations", json=VALID_REQUEST)
    second = client.post("/api/v1/simulations", json=VALID_REQUEST)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()


def test_replay_does_not_duplicate_events(client: TestClient) -> None:
    client.post("/api/v1/simulations", json=VALID_REQUEST)
    client.post("/api/v1/simulations", json=VALID_REQUEST)

    page = client.get("/api/v1/events", params={"limit": 1}).json()

    assert page["total"] == 30 * EVENTS_PER_TICK


def test_custom_start_time_and_interval_are_used(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={
            "scenario": "cpu_saturation",
            "seed": 3,
            "ticks": 6,
            "tick_interval_seconds": 30,
            "start_time": "2026-06-01T10:00:00+02:00",
        },
    )

    assert response.status_code == 201
    assert response.json()["start_time"] == "2026-06-01T08:00:00Z"
    # 6 ticks at 30 s: fault covers ticks [2, 4), i.e. 08:01:00 up to 08:02:00.
    assert response.json()["anomaly_window"] == {
        "start": "2026-06-01T08:01:00Z",
        "end": "2026-06-01T08:02:00Z",
    }


def test_different_seed_gives_different_run_id(client: TestClient) -> None:
    first = client.post("/api/v1/simulations", json=VALID_REQUEST).json()
    second = client.post(
        "/api/v1/simulations", json={**VALID_REQUEST, "seed": 8}
    ).json()

    assert first["run_id"] != second["run_id"]


def test_unknown_scenario_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations", json={**VALID_REQUEST, "scenario": "meteor_strike"}
    )

    assert response.status_code == 422


def test_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations", json={**VALID_REQUEST, "debug": True}
    )

    assert response.status_code == 422


def test_missing_seed_is_rejected(client: TestClient) -> None:
    payload = {key: value for key, value in VALID_REQUEST.items() if key != "seed"}

    response = client.post("/api/v1/simulations", json=payload)

    assert response.status_code == 422


def test_ticks_outside_bounds_are_rejected(client: TestClient) -> None:
    for ticks in (0, 289, -5):
        response = client.post(
            "/api/v1/simulations", json={**VALID_REQUEST, "ticks": ticks}
        )
        assert response.status_code == 422, ticks


def test_seed_outside_bounds_is_rejected(client: TestClient) -> None:
    for seed in (-1, 2**32):
        response = client.post(
            "/api/v1/simulations", json={**VALID_REQUEST, "seed": seed}
        )
        assert response.status_code == 422, seed


def test_naive_start_time_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={**VALID_REQUEST, "start_time": "2026-01-01T00:00:00"},
    )

    assert response.status_code == 422


def test_start_time_whose_timeline_overflows_is_rejected_with_422(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={**VALID_REQUEST, "start_time": "9999-12-31T23:45:00Z"},
    )

    assert response.status_code == 422


def test_start_time_before_calendar_start_is_rejected_with_422(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={**VALID_REQUEST, "start_time": "0001-01-01T00:00:00+01:00"},
    )

    assert response.status_code == 422


def test_timeline_ending_at_last_representable_instant_is_accepted(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={
            "scenario": "normal_operation",
            "seed": 1,
            "ticks": 1,
            "start_time": "9999-12-31T23:58:59.999999Z",
        },
    )

    assert response.status_code == 201
    assert response.json()["start_time"] == "9999-12-31T23:58:59.999999Z"


def test_timeline_running_past_last_instant_is_rejected(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={
            "scenario": "normal_operation",
            "seed": 1,
            "ticks": 1,
            "start_time": "9999-12-31T23:59:00Z",
        },
    )

    assert response.status_code == 422


def test_non_numeric_seed_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations", json={**VALID_REQUEST, "seed": "seven"}
    )

    assert response.status_code == 422


def test_malformed_json_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


def test_get_is_not_allowed_on_simulations(client: TestClient) -> None:
    response = client.get("/api/v1/simulations")

    assert response.status_code == 405
