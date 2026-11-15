"""The offline evaluation matrix: its denominators, its trace export, and its repeat.

The evaluator is loaded by path, as in the other evaluation tests. Every run here
uses the fake provider, so nothing reaches a network or a paid service.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.domain.agent_records import BudgetLimits
from app.domain.telemetry import ScenarioId

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_agent.py"
TRACE_KEYS = {"sequence", "kind", "outcome", "elapsed_ms", "tool", "failure_codes"}
MID_RUN_KINDS = [
    "provider_turn",
    "tool_outcome",
    "provider_turn",
    "provider_failure",
    "terminal",
]


def _load() -> ModuleType:
    """Import the evaluation script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("evaluate_agent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVAL = _load()


@pytest.fixture(scope="module")
def matrix() -> dict[str, Any]:
    """The full offline matrix, with traces kept, built once for this module."""
    return EVAL.evaluate(
        list(EVAL.SCENARIOS),
        list(EVAL.DEFAULT_SEEDS),
        EVAL.DEFAULT_TICKS,
        BudgetLimits(),
        keep_traces=True,
    )


def test_the_matrix_has_240_cases_with_separate_denominators(
    matrix: dict[str, Any],
) -> None:
    totals = matrix["totals"]

    assert totals["cases"] == 240
    assert totals["verified_completion"] == {
        "designed": 24,
        "verified": 24,
        "met": 24,
    }
    assert totals["expected_failure_handling"] == {"designed": 216, "met": 216}


def test_each_scenario_has_six_completing_and_fifty_four_stopping_cases(
    matrix: dict[str, Any],
) -> None:
    for name, item in matrix["scenarios"].items():
        assert item["verified_completion"]["designed"] == 6, name
        assert item["verified_completion"]["met"] == 6, name
        assert item["expected_failure_handling"]["designed"] == 54, name
        assert item["expected_failure_handling"]["met"] == 54, name


def test_no_faulty_answer_is_accepted_and_the_168_are_all_rejected(
    matrix: dict[str, Any],
) -> None:
    totals = matrix["totals"]

    assert totals["attacks"] == 168
    assert totals["attacks_rejected"] == 168
    assert totals["attacks_accepted"] == 0
    assert totals["novel_statements_accepted"] == 0


def test_fake_runs_count_turns_and_send_no_transport_requests(
    matrix: dict[str, Any],
) -> None:
    totals = matrix["totals"]

    assert totals["provider_turns"] > 0
    assert totals["transport_requests"] == 0


def test_the_mid_run_failure_keeps_one_tool_success_in_every_pair(
    matrix: dict[str, Any],
) -> None:
    mid_run = [
        trace
        for trace in matrix["traces"]
        if trace["case"] == "provider_failure_after_tool"
    ]

    assert len(mid_run) == 12
    for trace in mid_run:
        assert trace["status"] == "provider_error"
        assert [event["kind"] for event in trace["events"]] == MID_RUN_KINDS
        assert trace["events"][1]["outcome"] == "ok"


def test_an_exported_trace_holds_only_the_safe_fields(matrix: dict[str, Any]) -> None:
    keys = {
        key
        for trace in matrix["traces"]
        for event in trace["events"]
        for key in event
    }

    assert keys == TRACE_KEYS


def test_exported_traces_carry_timing_from_the_trace_clock(
    matrix: dict[str, Any],
) -> None:
    for trace in matrix["traces"]:
        for event in trace["events"]:
            assert isinstance(event["elapsed_ms"], int)
            assert event["elapsed_ms"] >= 0


def test_no_trace_is_exported_unless_asked_for() -> None:
    result = EVAL.evaluate(
        [ScenarioId.CPU_SATURATION],
        [601],
        EVAL.DEFAULT_TICKS,
        BudgetLimits(),
    )

    assert "traces" not in result


def test_trace_needs_json_and_applies_only_to_the_offline_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert EVAL.main(["--trace"]) == EVAL.EXIT_CONFIGURATION
    assert "needs --json" in capsys.readouterr().err


def test_json_output_adds_traces_only_with_the_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--json", "--scenarios", "cpu_saturation", "--seeds", "601"]

    assert EVAL.main(argv) == EVAL.EXIT_CLEAN
    plain = json.loads(capsys.readouterr().out)
    assert "traces" not in plain

    assert EVAL.main([*argv, "--trace"]) == EVAL.EXIT_CLEAN
    traced = json.loads(capsys.readouterr().out)
    assert len(traced["traces"]) == 20
    assert traced["totals"]["verified_completion"]["designed"] == 2


def test_human_output_states_both_denominators_and_that_it_is_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert EVAL.main(["--scenarios", "cpu_saturation", "--seeds", "601"]) == 0

    out = capsys.readouterr().out
    assert "complete" in out and "stop ok" in out
    assert "offline, fake provider" in out
    assert "2/2" in out
    assert "18/18" in out


def _stable(result: dict[str, Any]) -> dict[str, Any]:
    """The parts of a result that must repeat: no timing, no run-specific values."""
    totals = {
        key: value
        for key, value in result["totals"].items()
        if key != "mean_latency_ms"
    }
    traces = [
        {
            **trace,
            "events": [
                {key: value for key, value in event.items() if key != "elapsed_ms"}
                for event in trace["events"]
            ],
        }
        for trace in result["traces"]
    ]
    return {"totals": totals, "traces": traces}


def test_the_offline_matrix_repeats_outcomes_counts_and_event_order() -> None:
    first, second = (
        EVAL.evaluate(
            [ScenarioId.API_ERRORS],
            [602],
            EVAL.DEFAULT_TICKS,
            BudgetLimits(),
            keep_traces=True,
        )
        for _ in range(2)
    )

    assert _stable(first) == _stable(second)
