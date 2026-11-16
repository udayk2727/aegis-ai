"""Tests for hosted demo mode: snapshot verification, startup refusal, and the API.

The committed snapshot is regenerated here, so these tests also prove that it is
reproducible. Tampered copies are written under pytest's temporary directory.
"""

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.main import create_app
from app.services.baseline_trust import BaselineApproval, TrustedBaselines
from app.services.event_store import EventStore
from app.services.hosted_demo import (
    HostedDemo,
    HostedDemoError,
    build_snapshot_document,
    load_hosted_demo,
    store_hosted_runs,
)
from app.services.simulation import SimulationSpec, run_simulation

SNAPSHOT = Path(__file__).resolve().parents[2] / "demo" / "hosted_snapshot.json"
DEMO = "/api/v1/demo/baselines"
INCIDENTS = "/api/v1/incidents"


def _hosted(path: Path = SNAPSHOT) -> Settings:
    return Settings(
        environment=Environment.PRODUCTION,
        hosted_demo_enabled=True,
        hosted_demo_snapshot_path=path,
    )


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return document


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_hosted())) as test_client:
        yield test_client


def test_committed_snapshot_is_reproducible() -> None:
    assert _document() == build_snapshot_document()


def test_snapshot_loads_the_two_baselines_and_the_fault_run() -> None:
    demo = load_hosted_demo(SNAPSHOT)

    assert [run.spec.seed for run in demo.runs] == [301, 302, 311]
    assert [approval.spec.seed for approval in demo.trusted.approvals] == [301, 302]


def test_snapshot_lists_no_events_or_other_runs() -> None:
    document = _document()

    assert set(document) == {"data_origin", "runs", "version"}
    for entry in document["runs"]:
        assert set(entry) == {
            "anomalous_event_count",
            "approval",
            "event_count",
            "events_sha256",
            "run_id",
            "spec",
        }


def test_hosted_listing_serves_the_two_fixed_baselines(client: TestClient) -> None:
    response = client.get(DEMO)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["seed"] for item in body["items"]} == {301, 302}


def test_hosted_mode_refuses_the_simulation_route(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulations",
        json={"scenario": "normal_operation", "seed": 1, "ticks": 10},
    )

    assert response.status_code == 404


def test_hosted_mode_lists_the_cpu_incident_with_unverified_rank_one(
    client: TestClient,
) -> None:
    baselines = client.get(DEMO).json()["items"]
    query = [("baseline_run_id", item["run_id"]) for item in baselines]

    incidents = client.get(INCIDENTS, params=query).json()["items"]
    assert len(incidents) == 1
    investigation = client.get(
        f"{INCIDENTS}/{incidents[0]['incident_id']}/investigation", params=query
    ).json()

    rank_one = investigation["hypotheses"][0]
    assert rank_one["kind"] == "cpu_resource_saturation"
    assert rank_one["verification_status"] == "unverified"
    assert investigation["root_cause_assessment"] == "not_assessed"


def test_a_missing_snapshot_refuses_startup(tmp_path: Path) -> None:
    with pytest.raises(HostedDemoError, match="cannot be read"):
        create_app(_hosted(tmp_path / "absent.json"))


def test_an_unparseable_snapshot_refuses_startup(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(HostedDemoError, match="cannot be read"):
        create_app(_hosted(path))


def test_an_unsupported_version_refuses_startup(tmp_path: Path) -> None:
    document = _document()
    document["version"] = 2

    with pytest.raises(HostedDemoError, match="version"):
        create_app(_hosted(_write(tmp_path, document)))


def test_a_snapshot_not_marked_synthetic_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["data_origin"] = "production"

    with pytest.raises(HostedDemoError, match="synthetic"):
        create_app(_hosted(_write(tmp_path, document)))


def _tamper(
    tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> Path:
    document = copy.deepcopy(_document())
    change(document)
    return _write(tmp_path, document)


def _first_baseline(document: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = document["runs"][0]
    return entry


def _fault_run(document: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = document["runs"][2]
    return entry


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["runs"].pop(), "fixed runs"),
        (lambda d: d["runs"].append(d["runs"][0]), "fixed runs"),
        (lambda d: _first_baseline(d).update(event_count=2591), "event count"),
        (
            lambda d: _first_baseline(d).update(anomalous_event_count=1),
            "anomaly count",
        ),
        (
            lambda d: _first_baseline(d).update(events_sha256="0" * 64),
            "event digest",
        ),
        (lambda d: _first_baseline(d).update(run_id="sim-0000000000000000"), "run ID"),
        (
            lambda d: _first_baseline(d)["approval"].update(checked_event_count=1),
            "approval",
        ),
        (lambda d: _first_baseline(d).update(approval=None), "approval"),
        (
            lambda d: _first_baseline(d)["approval"].update(method="reviewed_by_hand"),
            "approval",
        ),
        (
            lambda d: _fault_run(d).update(
                approval={"method": "synthetic_ground_truth", "checked_event_count": 1}
            ),
            "carries an approval",
        ),
        (
            lambda d: _first_baseline(d)["spec"].update(seed=999),
            "outside the fixed set",
        ),
    ],
)
def test_a_snapshot_that_does_not_verify_refuses_startup(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    with pytest.raises(HostedDemoError, match=message):
        create_app(_hosted(_tamper(tmp_path, change)))


def test_approvals_must_match_the_stored_baselines() -> None:
    demo = load_hosted_demo(SNAPSHOT)
    stray = BaselineApproval(
        run_id="sim-0000000000000000",
        spec=SimulationSpec(scenario="normal_operation", seed=301, ticks=288),
        method="synthetic_ground_truth",
        checked_event_count=2592,
    )
    mismatched = HostedDemo(runs=demo.runs, trusted=TrustedBaselines((stray,)))

    with pytest.raises(HostedDemoError, match="not stored with its spec"):
        store_hosted_runs(EventStore(), mismatched)


def test_stored_runs_match_the_simulator_for_each_approval() -> None:
    demo = load_hosted_demo(SNAPSHOT)
    store = EventStore()

    store_hosted_runs(store, demo)

    for approval in demo.trusted.approvals:
        stored = {run.run_id: run for run in store.runs()}[approval.run_id]
        assert stored == run_simulation(approval.spec)
