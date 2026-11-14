"""Tests for the offline agent evaluation: rejection, labels, cost, and the live gate.

The evaluation uses the fake provider only. Live mode is tested for its refusals,
and no test reaches the live path.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.domain.agent_records import AgentStatus, BudgetLimits
from app.domain.telemetry import ScenarioId
from app.services.agent_budget import RunBudget
from app.services.agent_orchestration import run_agent
from tests.agent_fixtures import fault_fixture

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_agent.py"


def _load() -> ModuleType:
    """Import the evaluation script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("evaluate_agent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVAL = _load()
FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id


def _cases() -> list[Any]:
    """Every case for the test report, built from the evaluation's own builder."""
    return EVAL.build_cases(REPORT, EVENTS, BudgetLimits())


def test_every_faulty_answer_is_rejected_and_none_is_accepted() -> None:
    attacks = [case for case in _cases() if case.kind == "attack"]

    outcomes = [EVAL.run_case(case, REPORT, EVENTS) for case in attacks]

    assert len(attacks) >= 10
    for outcome in outcomes:
        assert outcome.status is not AgentStatus.VERIFIED, outcome.name
        assert outcome.met_expectation, outcome.name
        assert outcome.run.claims == (), outcome.name


def test_the_reference_answer_verifies_and_quotes_only_canonical_text() -> None:
    reference = next(case for case in _cases() if case.name == "reference")

    outcome = EVAL.run_case(reference, REPORT, EVENTS)

    assert outcome.status is AgentStatus.VERIFIED
    assert outcome.outside_report == 0
    assert outcome.run.claims


def test_the_model_never_receives_simulator_labels() -> None:
    reference = next(case for case in _cases() if case.name == "reference")
    provider = reference.provider()

    run_agent(REPORT, EVENTS, provider, budget=RunBudget(BudgetLimits()))

    sent = "\n".join(
        message.content for transcript in provider.transcripts for message in transcript
    )
    for forbidden in ("is_anomalous", "scenario_id", '"label"', "anomaly_window"):
        assert forbidden not in sent


def test_cost_is_reported_as_unknown_for_the_fake_provider() -> None:
    reference = next(case for case in _cases() if case.name == "reference")

    outcome = EVAL.run_case(reference, REPORT, EVENTS)

    assert outcome.run.accounting is not None
    assert outcome.run.accounting.estimated_cost_usd is None


def test_fake_token_counts_are_estimates_and_are_recorded() -> None:
    reference = next(case for case in _cases() if case.name == "reference")

    outcome = EVAL.run_case(reference, REPORT, EVENTS)

    assert outcome.run.token_usage.total > 0


def test_the_tool_budget_case_stops_at_the_tool_limit() -> None:
    budget_case = next(
        case for case in _cases() if case.name == "tool_budget_exhausted"
    )

    outcome = EVAL.run_case(budget_case, REPORT, EVENTS)

    assert outcome.status is AgentStatus.BUDGET_EXHAUSTED
    assert outcome.run.budget_stop is not None


def test_bad_tool_requests_are_counted_and_the_run_still_verifies() -> None:
    tools_case = next(case for case in _cases() if case.name == "bad_tool_requests")

    outcome = EVAL.run_case(tools_case, REPORT, EVENTS)

    assert outcome.status is AgentStatus.VERIFIED
    assert outcome.run.tool_rejections == 2


def test_scenario_agreement_is_a_label_check_on_verified_runs() -> None:
    reference = next(case for case in _cases() if case.name == "reference")
    outcome = EVAL.run_case(reference, REPORT, EVENTS)

    assert EVAL.scenario_agreement(ScenarioId.CPU_SATURATION, outcome.run) is True


def test_the_default_offline_run_exits_clean_and_reports_no_safety_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = EVAL.main(["--scenarios", "cpu_saturation", "--seeds", "601", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert code == EVAL.EXIT_CLEAN
    assert result["totals"]["attacks_accepted"] == 0
    assert result["totals"]["novel_statements_accepted"] == 0
    assert result["totals"]["cost_usd"] is None
    assert result["provider"] == "fake-deterministic"


def test_a_safety_failure_is_reported_with_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_evaluate = EVAL.evaluate

    def unsafe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """The real result, with one fabricated claim recorded as accepted."""
        result = real_evaluate(*args, **kwargs)
        result["totals"]["attacks_accepted"] = 1
        return result

    monkeypatch.setattr(EVAL, "evaluate", unsafe)

    code = EVAL.main(["--scenarios", "cpu_saturation", "--seeds", "601"])

    assert code == EVAL.EXIT_SAFETY_FAILURE


def test_live_mode_is_refused_without_the_live_flag_and_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def must_not_build() -> None:
        raise AssertionError("no live provider may be built without --live")

    monkeypatch.setattr(EVAL, "live_provider_factory", must_not_build)

    code = EVAL.main(["--provider", "openai"])

    assert code == EVAL.EXIT_CONFIGURATION
    assert "--live" in capsys.readouterr().err


def test_live_mode_is_refused_for_the_fake_provider_with_live_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = EVAL.main(["--live"])

    assert code == EVAL.EXIT_CONFIGURATION
    assert "applies only to a live provider" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "13"])
def test_the_live_case_cap_is_bounded(
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = EVAL.main(["--provider", "openai", "--live", "--max-live-cases", value])

    assert code == EVAL.EXIT_CONFIGURATION
    assert "--max-live-cases" in capsys.readouterr().err


def test_live_mode_without_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_LLM_MODEL", raising=False)

    code = EVAL.main(["--provider", "openai", "--live"])

    assert code == EVAL.EXIT_CONFIGURATION
    assert "AEGIS_LLM_MODEL is not set" in capsys.readouterr().err


def test_an_out_of_range_live_budget_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = EVAL.main(["--provider", "openai", "--live", "--max-requests", "99"])

    assert code == EVAL.EXIT_CONFIGURATION
    assert "outside its allowed range" in capsys.readouterr().err
