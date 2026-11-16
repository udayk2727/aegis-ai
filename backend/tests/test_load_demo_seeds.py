"""Tests for the demo seed loader: readiness, the run order, and the listing check.

Every request goes through an injected fake, so no test opens a socket or waits
on the real clock.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "load_demo_seeds.py"
BASE = "http://api:8000"


def _load() -> ModuleType:
    """Import the seed script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("load_demo_seeds", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SEED = _load()


class FakeApi:
    """Records each request and answers from a scripted queue per route."""

    def __init__(self, answers: dict[str, list[tuple[int, Any]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(
        self, method: str, url: str, body: dict[str, Any] | None
    ) -> tuple[int, Any]:
        self.calls.append((method, url, body))
        queue = self.answers[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _created(run_id: str) -> tuple[int, Any]:
    return 201, {"run_id": run_id}


def test_readiness_waits_until_health_answers_ok() -> None:
    api = FakeApi({f"{BASE}/health": [(0, None), (503, None), (200, {"status": "ok"})]})
    sleeps: list[float] = []

    ready = SEED.wait_until_ready(BASE, api, sleeps.append, attempts=5)

    assert ready is True
    assert len(api.calls) == 3
    assert sleeps == [SEED.READY_INTERVAL_SECONDS] * 2


def test_readiness_gives_up_after_the_attempt_budget() -> None:
    api = FakeApi({f"{BASE}/health": [(0, None)]})
    sleeps: list[float] = []

    ready = SEED.wait_until_ready(BASE, api, sleeps.append, attempts=3)

    assert ready is False
    assert len(api.calls) == 3


def test_runs_are_created_in_the_documented_order_and_ids_returned() -> None:
    api = FakeApi(
        {
            f"{BASE}/api/v1/simulations": [
                _created("run-a"),
                _created("run-b"),
                _created("run-c"),
            ],
        }
    )

    run_ids = SEED.load_runs(BASE, api)

    assert run_ids == ["run-a", "run-b", "run-c"]
    assert [call[2] for call in api.calls] == [
        {"scenario": "normal_operation", "seed": 301, "ticks": 288},
        {"scenario": "normal_operation", "seed": 302, "ticks": 288},
        {"scenario": "cpu_saturation", "seed": 311, "ticks": 288},
    ]


def test_a_repeated_run_answered_with_200_is_accepted() -> None:
    api = FakeApi(
        {
            f"{BASE}/api/v1/simulations": [
                (200, {"run_id": "run-a"}),
                (200, {"run_id": "run-b"}),
                (200, {"run_id": "run-c"}),
            ],
        }
    )

    assert SEED.load_runs(BASE, api) == ["run-a", "run-b", "run-c"]


def test_a_refused_run_stops_the_load_and_names_the_step() -> None:
    api = FakeApi(
        {
            f"{BASE}/api/v1/simulations": [
                _created("run-a"),
                (404, None),
            ],
        }
    )

    with pytest.raises(SEED.SeedError) as failure:
        SEED.load_runs(BASE, api)

    assert "seed 302" in str(failure.value)
    assert "status 404" in str(failure.value)
    assert len(api.calls) == 2


def test_the_listing_count_comes_from_the_demo_endpoint() -> None:
    api = FakeApi(
        {f"{BASE}/api/v1/demo/baselines": [(200, {"items": [], "total": 2})]}
    )

    assert SEED.count_baselines(BASE, api) == 2


def test_a_listing_that_is_not_served_is_a_seed_error() -> None:
    api = FakeApi({f"{BASE}/api/v1/demo/baselines": [(404, None)]})

    with pytest.raises(SEED.SeedError, match="status 404"):
        SEED.count_baselines(BASE, api)


def test_main_reports_success_when_the_listing_shows_two_baselines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = {
        f"{BASE}/health": [(200, {"status": "ok"})],
        f"{BASE}/api/v1/simulations": [
            _created("run-a"),
            _created("run-b"),
            _created("run-c"),
        ],
        f"{BASE}/api/v1/demo/baselines": [(200, {"items": [], "total": 2})],
    }
    monkeypatch.setattr(SEED, "_http", FakeApi(answers))

    code = SEED.main(["--base-url", BASE])

    assert code == 0
    assert "shows 2 baselines" in capsys.readouterr().out


def test_main_fails_when_the_listing_shows_the_wrong_number(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = {
        f"{BASE}/health": [(200, {"status": "ok"})],
        f"{BASE}/api/v1/simulations": [_created("run-a")],
        f"{BASE}/api/v1/demo/baselines": [(200, {"items": [], "total": 1})],
    }
    monkeypatch.setattr(SEED, "_http", FakeApi(answers))

    code = SEED.main(["--base-url", BASE])

    assert code == 1
    assert "expected 2" in capsys.readouterr().err


def test_watch_reloads_the_runs_when_the_listing_is_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeApi(
        {
            f"{BASE}/api/v1/demo/baselines": [
                (200, {"items": [], "total": 0}),
                (200, {"items": [], "total": 2}),
            ],
            f"{BASE}/api/v1/simulations": [_created("run-a")],
        }
    )
    sleeps: list[float] = []

    SEED.keep_demo_loaded(BASE, api, sleeps.append, rounds=2)

    posts = [call for call in api.calls if call[0] == "POST"]
    assert len(posts) == 3
    assert sleeps == [SEED.WATCH_INTERVAL_SECONDS] * 2
    assert "reloaded the demo runs" in capsys.readouterr().out


def test_watch_leaves_a_loaded_listing_alone() -> None:
    api = FakeApi(
        {f"{BASE}/api/v1/demo/baselines": [(200, {"items": [], "total": 2})]}
    )

    SEED.keep_demo_loaded(BASE, api, lambda _seconds: None, rounds=3)

    assert [call[0] for call in api.calls] == ["GET", "GET", "GET"]


def test_watch_keeps_running_while_the_api_is_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeApi(
        {
            f"{BASE}/api/v1/demo/baselines": [
                (0, None),
                (200, {"items": [], "total": 2}),
            ],
        }
    )

    SEED.keep_demo_loaded(BASE, api, lambda _seconds: None, rounds=2)

    assert len(api.calls) == 2
    assert "status 0" in capsys.readouterr().err
