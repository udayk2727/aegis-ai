"""Tests for the local agent CLI: exit codes, JSON output, budgets, and live opt-in.

The live path is exercised only through a stand-in provider. No test builds the
real SDK client or makes a request.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.domain.agent_records import (
    AgentStatus,
    BudgetLimits,
    ProviderFailureReason,
    ProviderTurn,
)
from app.domain.telemetry import ScenarioId
from app.services.agent_budget import RunBudget
from app.services.agent_provider import ProviderError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_investigation_agent.py"


def _load_cli() -> ModuleType:
    """Import the script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("run_investigation_agent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()


def _demo(scenario: ScenarioId = ScenarioId.CPU_SATURATION, **options: Any) -> Any:
    """Run the demo with default baselines and the given limits."""
    return CLI.run_demo(
        scenario,
        seed=601,
        ticks=60,
        baseline_seeds=CLI.DEFAULT_BASELINE_SEEDS,
        limits=options.pop("limits", BudgetLimits()),
        **options,
    )


@pytest.fixture
def no_incidents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the pipeline find no incident, without depending on a seed that has none."""
    monkeypatch.setattr(CLI, "build_incidents", lambda *args, **kwargs: [])


def test_default_demo_produces_a_verified_run() -> None:
    run = _demo()

    assert run is not None
    assert run.status is AgentStatus.VERIFIED
    assert run.provider_id == "fake-deterministic"
    assert run.accounting is not None
    assert run.accounting.final_status is AgentStatus.VERIFIED


def test_a_fault_with_no_incident_returns_none(no_incidents: None) -> None:
    assert _demo() is None


def test_main_prints_the_verified_run_as_json_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = CLI.main(["--scenario", "cpu_saturation", "--seed", "601"])

    printed = json.loads(capsys.readouterr().out)
    assert code == CLI.EXIT_VERIFIED
    assert printed["status"] == "verified"
    assert printed["verification_status"] == "verified"
    assert printed["failures"] == []
    assert printed["accounting"]["estimated_cost_usd"] is None


def test_main_exits_one_when_no_incident_is_detected(
    no_incidents: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = CLI.main(["--scenario", "cpu_saturation", "--seed", "601"])

    assert code == CLI.EXIT_NO_INCIDENT
    assert "no incident" in capsys.readouterr().err


def test_normal_operation_is_not_an_available_fault_scenario() -> None:
    with pytest.raises(SystemExit):
        CLI.main(["--scenario", ScenarioId.NORMAL_OPERATION.value])


def test_an_unknown_provider_is_not_offered() -> None:
    with pytest.raises(SystemExit):
        CLI.main(["--provider", "gemini"])


@pytest.mark.parametrize(
    "flags",
    [
        ["--max-total-tokens", "0"],
        ["--max-seconds", "0"],
        ["--max-requests", "0"],
        ["--max-requests", "21"],
        ["--request-timeout", "121"],
        ["--max-seconds", "901"],
        ["--max-retries", "4"],
        ["--max-output-tokens", "4001"],
        ["--max-tool-calls", "-1"],
    ],
)
def test_budget_values_outside_their_bounds_are_configuration_errors(
    flags: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = CLI.main(flags)

    assert code == CLI.EXIT_CONFIGURATION
    assert "outside its allowed range" in capsys.readouterr().err


def test_budget_error_names_the_flag_and_never_the_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    CLI.main(["--max-total-tokens", "999999999"])

    err = capsys.readouterr().err
    assert "--max-total-tokens" in err
    assert "999999999" not in err


def test_the_openai_provider_needs_the_live_flag_and_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def must_not_build() -> None:
        raise AssertionError("the live provider must not be built without --live")

    monkeypatch.setattr(CLI, "live_provider_factory", must_not_build)

    code = CLI.main(["--provider", "openai"])

    assert code == CLI.EXIT_CONFIGURATION
    assert "--live" in capsys.readouterr().err


def test_live_is_refused_for_the_fake_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = CLI.main(["--live"])

    assert code == CLI.EXIT_CONFIGURATION
    assert "applies only to a live provider" in capsys.readouterr().err


def test_a_live_run_with_an_out_of_range_budget_is_refused_before_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_build() -> None:
        raise AssertionError("no provider may be built with an invalid budget")

    monkeypatch.setattr(CLI, "live_provider_factory", must_not_build)

    code = CLI.main(["--provider", "openai", "--live", "--max-requests", "50"])

    assert code == CLI.EXIT_CONFIGURATION


def test_live_without_configuration_fails_closed_and_names_no_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_LLM_MODEL", raising=False)

    code = CLI.main(["--provider", "openai", "--live"])

    err = capsys.readouterr().err
    assert code == CLI.EXIT_CONFIGURATION
    assert "AEGIS_LLM_MODEL is not set" in err


def test_a_missing_pricing_file_is_a_configuration_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = CLI.main(["--pricing-file", str(tmp_path / "absent.json")])

    assert code == CLI.EXIT_CONFIGURATION
    assert "pricing file" in capsys.readouterr().err


def test_live_wiring_is_reached_only_through_the_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stand-in live provider that fails. It cannot make any request."""

    class UnavailableProvider:
        """Stands in for the live provider. It fails before any request."""

        provider_id = "openai-responses:stand-in"

        def next_turn(self, transcript: object) -> ProviderTurn:
            """Fail with a fixed reason, as an unreachable service would."""
            raise ProviderError(ProviderFailureReason.NETWORK)

    built: list[object] = []

    def stand_in_build(
        settings: object,
        budget: object = None,
        answer_format: object = None,
    ) -> UnavailableProvider:
        built.append((settings, budget, answer_format))
        return UnavailableProvider()

    pytest.importorskip("openai", reason="the optional OpenAI SDK is not installed")
    from app.services import agent_openai_provider

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("AEGIS_LLM_MODEL", "test-model")
    monkeypatch.setattr(agent_openai_provider, "build_live_provider", stand_in_build)

    code = CLI.main(["--provider", "openai", "--live", "--scenario", "cpu_saturation"])

    printed = json.loads(capsys.readouterr().out)
    assert code == CLI.EXIT_REFUSED
    assert len(built) == 1
    assert isinstance(built[0][1], RunBudget)
    assert printed["status"] == "provider_error"
    assert printed["provider_failure"] == "network"
    assert "test-key-not-real" not in json.dumps(printed)


def test_the_anthropic_provider_needs_the_live_flag_and_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def must_not_build() -> None:
        raise AssertionError("the Anthropic provider must not be built without --live")

    monkeypatch.setattr(CLI, "anthropic_provider_factory", must_not_build)

    code = CLI.main(["--provider", "anthropic"])

    assert code == CLI.EXIT_CONFIGURATION
    assert "anthropic provider makes paid requests" in capsys.readouterr().err


def test_anthropic_live_wiring_is_reached_only_through_the_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stand-in Anthropic provider that fails. It cannot make any request."""

    class UnavailableProvider:
        """Stands in for the Anthropic provider. It fails before any request."""

        provider_id = "anthropic-messages:stand-in"

        def next_turn(self, transcript: object) -> ProviderTurn:
            """Fail with a fixed reason, as an unreachable service would."""
            raise ProviderError(ProviderFailureReason.NETWORK)

    built: list[object] = []

    def stand_in_build(
        settings: object,
        budget: object = None,
        answer_format: object = None,
    ) -> UnavailableProvider:
        built.append((settings, budget, answer_format))
        return UnavailableProvider()

    pytest.importorskip(
        "anthropic", reason="the optional Anthropic SDK is not installed"
    )
    from app.services import agent_anthropic_provider as anthropic_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("AEGIS_LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(anthropic_provider, "build_live_provider", stand_in_build)

    code = CLI.main(
        ["--provider", "anthropic", "--live", "--scenario", "cpu_saturation"]
    )

    printed = json.loads(capsys.readouterr().out)
    assert code == CLI.EXIT_REFUSED
    assert len(built) == 1
    assert isinstance(built[0][1], RunBudget)
    assert printed["provider_id"] == "anthropic-messages:stand-in"
    assert printed["provider_failure"] == "network"
    assert "test-key-not-real" not in json.dumps(printed)


def test_anthropic_live_without_its_key_fails_closed_naming_only_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AEGIS_LLM_MODEL", "claude-sonnet-5-5")

    code = CLI.main(["--provider", "anthropic", "--live"])

    err = capsys.readouterr().err
    assert code == CLI.EXIT_CONFIGURATION
    assert "ANTHROPIC_API_KEY is not set" in err


def test_the_compact_answer_format_is_verified_through_the_fake_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = CLI.main(
        ["--scenario", "cpu_saturation", "--answer-format", "compact"],
    )

    printed = json.loads(capsys.readouterr().out)
    assert code == CLI.EXIT_VERIFIED
    assert printed["status"] == "verified"


def test_an_unknown_answer_format_is_refused_before_any_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as refused:
        CLI.main(["--answer-format", "partial"])

    assert refused.value.code == 2
    assert capsys.readouterr().out == ""
