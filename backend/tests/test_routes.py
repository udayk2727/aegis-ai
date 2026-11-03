"""Regression tests for route registration across all feature routers."""

from fastapi.testclient import TestClient


def test_all_public_routes_are_registered(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])

    assert paths == {
        "/",
        "/health",
        "/api/v1/simulations",
        "/api/v1/events",
        "/api/v1/analytics/summary",
        "/api/v1/analytics/baselines",
        "/api/v1/analytics/timeseries",
    }


def test_every_route_declares_a_response_model(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            responses = operation["responses"]
            success = responses.get("201", responses.get("200"))
            assert success is not None, f"{method} {path} has no success response"
            assert "content" in success, f"{method} {path} has no response body"
