"""Tests for the read-only analytics endpoints: contract, validation, and bounds."""

import statistics
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.simulation import EVENTS_PER_TICK

SPIKE_RUN: dict[str, Any] = {
    "scenario": "database_latency_spike",
    "seed": 7,
    "ticks": 30,
}
NORMAL_RUN: dict[str, Any] = {
    "scenario": "normal_operation",
    "seed": 3,
    "ticks": 30,
}


def _create(client: TestClient, payload: dict[str, Any]) -> str:
    """Store a run through the public API and return its run ID."""
    response = client.post("/api/v1/simulations", json=payload)
    assert response.status_code in (200, 201)
    run_id: str = response.json()["run_id"]
    return run_id


def _timeseries(client: TestClient, run_id: str, **params: Any) -> dict[str, Any]:
    """Request a timeseries page with the default query unless overridden."""
    query: dict[str, Any] = {
        "run_id": run_id,
        "service": "orders-db",
        "metric_name": "db_query_latency_ms",
    }
    query.update(params)
    response = client.get("/api/v1/analytics/timeseries", params=query)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_summary_on_empty_store_returns_no_series(client: TestClient) -> None:
    response = client.get("/api/v1/analytics/summary")

    assert response.status_code == 200
    assert response.json()["event_count"] == 0
    assert response.json()["series"] == []


def test_baselines_on_empty_store_return_zero_samples(client: TestClient) -> None:
    body = client.get("/api/v1/analytics/baselines").json()

    assert body["sample_count"] == 0
    assert body["source_run_ids"] == []
    assert body["series"] == []


def test_timeseries_for_unknown_run_is_an_empty_page(client: TestClient) -> None:
    body = _timeseries(client, "sim-0123456789abcdef")

    assert body["total"] == 0
    assert body["points"] == []


def test_summary_counts_and_separates_normal_from_incident(
    client: TestClient,
) -> None:
    run_id = _create(client, SPIKE_RUN)

    everything = client.get(
        "/api/v1/analytics/summary", params={"run_id": run_id}
    ).json()
    incident = client.get(
        "/api/v1/analytics/summary",
        params={"run_id": run_id, "is_anomalous": "true"},
    ).json()
    normal = client.get(
        "/api/v1/analytics/summary",
        params={"run_id": run_id, "is_anomalous": "false"},
    ).json()

    assert everything["event_count"] == 30 * EVENTS_PER_TICK
    assert incident["event_count"] == 10 * EVENTS_PER_TICK
    assert normal["event_count"] == 20 * EVENTS_PER_TICK
    assert len(everything["series"]) == 9


def test_summary_series_carry_service_metric_and_unit(client: TestClient) -> None:
    _create(client, NORMAL_RUN)

    series = client.get("/api/v1/analytics/summary").json()["series"]
    cpu = [
        item
        for item in series
        if item["metric_name"] == "cpu_utilization_percent"
        and item["service"] == "orders-db"
    ]

    assert len(cpu) == 1
    assert cpu[0]["unit"] == "percent"
    assert cpu[0]["sample_count"] == 30


def test_baselines_use_only_normal_events(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    body = client.get("/api/v1/analytics/baselines").json()

    assert body["sample_count"] == 20 * EVENTS_PER_TICK
    assert body["source_run_ids"] == [run_id]
    assert body["methodology"] == {"quantile": "linear", "stdev": "sample"}


def test_baselines_filter_by_run_and_cutoff(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    other_run = client.get(
        "/api/v1/analytics/baselines",
        params={"run_id": "sim-fedcba9876543210"},
    ).json()
    before_fault = client.get(
        "/api/v1/analytics/baselines",
        params={"run_id": run_id, "before": "2026-01-01T00:05:00Z"},
    ).json()

    assert other_run["sample_count"] == 0
    # Ticks 0-4 are normal and fall before five minutes: five ticks of nine events.
    assert before_fault["sample_count"] == 5 * EVENTS_PER_TICK


def test_baselines_reject_more_than_twenty_runs(client: TestClient) -> None:
    run_ids = [f"sim-{index:016x}" for index in range(21)]

    response = client.get("/api/v1/analytics/baselines", params={"run_id": run_ids})

    assert response.status_code == 422


def test_baselines_reject_naive_cutoff(client: TestClient) -> None:
    response = client.get(
        "/api/v1/analytics/baselines",
        params={"before": "2026-01-01T00:05:00"},
    )

    assert response.status_code == 422


def test_timeseries_has_rolling_statistics_and_defaults(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    body = _timeseries(client, run_id)
    points = body["points"]

    assert body["total"] == 30
    assert body["window"] == 5
    assert body["unit"] == "milliseconds"
    assert [point["window_count"] for point in points[:5]] == [1, 2, 3, 4, 5]
    assert points[3]["mean"] is None
    assert points[4]["mean"] is not None
    values = [point["value"] for point in points]
    assert points[4]["mean"] == pytest.approx(statistics.fmean(values[0:5]))
    assert points[4]["stdev"] == pytest.approx(statistics.stdev(values[0:5]))


def test_timeseries_points_are_chronological_utc(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    points = _timeseries(client, run_id)["points"]
    timestamps = [point["timestamp"] for point in points]

    assert timestamps == sorted(timestamps)
    assert all(stamp.endswith("Z") for stamp in timestamps)


def test_timeseries_paging_matches_the_full_series(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    full = _timeseries(client, run_id)["points"]
    second_page = _timeseries(client, run_id, limit=5, offset=5)

    assert second_page["points"] == full[5:10]
    assert second_page["total"] == 30


def test_timeseries_offset_past_the_end_keeps_total(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    body = _timeseries(client, run_id, offset=500)

    assert body["points"] == []
    assert body["total"] == 30


def test_timeseries_for_unmatched_service_and_metric_is_empty(
    client: TestClient,
) -> None:
    run_id = _create(client, SPIKE_RUN)

    body = _timeseries(client, run_id, service="api-gateway")

    assert body["total"] == 0
    assert body["points"] == []


LABEL_KEYS = {"is_anomalous", "scenario_id", "label"}


def _keys(node: Any) -> set[str]:
    """Collect every object key in a parsed JSON document, at any depth."""
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for item in node for key in _keys(item)}
    return set()


def test_analytics_responses_contain_no_per_event_label_fields(
    client: TestClient,
) -> None:
    # Raw per-event labels are never returned. Filtering by is_anomalous or
    # scenario_id can still reveal label-derived aggregates; see the README.
    run_id = _create(client, SPIKE_RUN)

    responses = [
        client.get("/api/v1/analytics/summary", params={"run_id": run_id}),
        client.get("/api/v1/analytics/baselines"),
        client.get(
            "/api/v1/analytics/timeseries",
            params={
                "run_id": run_id,
                "service": "orders-db",
                "metric_name": "db_query_latency_ms",
            },
        ),
    ]

    for response in responses:
        assert _keys(response.json()).isdisjoint(LABEL_KEYS)


def test_timeseries_requires_run_service_and_metric(client: TestClient) -> None:
    response = client.get("/api/v1/analytics/timeseries", params={"run_id": "x"})

    assert response.status_code == 422


def test_timeseries_rejects_out_of_range_window_and_limit(client: TestClient) -> None:
    run_id = _create(client, SPIKE_RUN)

    for params in (
        {"window": 1},
        {"window": 61},
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
    ):
        response = client.get(
            "/api/v1/analytics/timeseries",
            params={
                "run_id": run_id,
                "service": "orders-db",
                "metric_name": "db_query_latency_ms",
                **params,
            },
        )
        assert response.status_code == 422, params


def test_summary_rejects_malformed_filters(client: TestClient) -> None:
    for params in (
        {"run_id": "not-a-run"},
        {"service": "API_GATEWAY"},
        {"metric_name": "made_up_metric"},
        {"is_anomalous": "maybe"},
    ):
        response = client.get("/api/v1/analytics/summary", params=params)
        assert response.status_code == 422, params
