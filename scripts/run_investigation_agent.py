"""Run the investigation agent on one simulated incident, locally.

The agent runs as a library. This script is a local-only wrapper around it. It
simulates approved baselines and one fault run, builds the incident and its
report, and runs the agent on that report.

The default provider is the deterministic fake, which makes no network call. The
OpenAI provider is used only with both ``--provider openai`` and ``--live``. Its
key and model come from the environment (``OPENAI_API_KEY`` and
``AEGIS_LLM_MODEL``). Each limit has a conservative default, and a flag can only
set a value within the hard maximum. Pytest, CI, and the evaluation scripts never
use the live provider.

    python scripts/run_investigation_agent.py
    python scripts/run_investigation_agent.py --scenario database_latency_spike
    python scripts/run_investigation_agent.py --provider openai --live

The output is a JSON ``AgentRun``, which includes its accounting. The exit code
is 0 when the answer is verified, 2 when it is refused, 1 when the fault run
produced no incident, and 3 when a setting is missing, invalid, or not allowed.
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from pydantic import ValidationError  # noqa: E402

from app.config import (  # noqa: E402
    ANTHROPIC_API_KEY_VARIABLE,
    ConfigurationError,
    load_live_model_settings,
)
from app.domain.agent_records import (  # noqa: E402
    AgentRun,
    AgentStatus,
    AnswerFormat,
    BudgetLimits,
)
from app.domain.investigation_records import Investigation  # noqa: E402
from app.domain.telemetry import ScenarioId, TelemetryEvent  # noqa: E402
from app.services.agent_budget import (  # noqa: E402
    PricingError,
    PricingTable,
    RunBudget,
    load_pricing,
)
from app.services.agent_fake_provider import (  # noqa: E402
    ScriptedProvider,
    compact_reference_turns,
    reference_turns,
)
from app.services.agent_orchestration import run_agent  # noqa: E402
from app.services.agent_provider import ModelProvider  # noqa: E402
from app.services.baseline_trust import TrustedBaselines  # noqa: E402
from app.services.incident_pipeline import (  # noqa: E402
    ObservedRun,
    build_incidents,
    score_run,
)
from app.services.investigation import investigate  # noqa: E402
from app.services.offline_validation import validate_synthetic_baseline  # noqa: E402
from app.services.simulation import SimulationSpec, run_simulation  # noqa: E402

DEFAULT_BASELINE_SEEDS = (101, 102, 103, 104)
DEFAULT_TICKS = 60
FAULT_SCENARIOS = tuple(
    scenario for scenario in ScenarioId if scenario is not ScenarioId.NORMAL_OPERATION
)
LIVE_PROVIDERS = ("openai", "anthropic")
EXIT_VERIFIED = 0
EXIT_NO_INCIDENT = 1
EXIT_REFUSED = 2
EXIT_CONFIGURATION = 3

ProviderFactory = Callable[[Investigation, RunBudget], ModelProvider]

BUDGET_FLAGS: dict[str, str] = {
    "max_requests": "--max-requests",
    "max_tool_calls": "--max-tool-calls",
    "max_total_tokens": "--max-total-tokens",
    "max_output_tokens": "--max-output-tokens",
    "request_timeout_seconds": "--request-timeout",
    "deadline_seconds": "--max-seconds",
    "max_retries": "--max-retries",
}


def fake_provider_factory(answer_format: AnswerFormat) -> ProviderFactory:
    """The deterministic provider, answering in ``answer_format``. No network call."""

    def make(report: Investigation, budget: RunBudget) -> ModelProvider:
        """Script the faithful reader's turns in the requested answer format."""
        if answer_format is AnswerFormat.COMPACT:
            return ScriptedProvider(compact_reference_turns(report))
        return ScriptedProvider(reference_turns(report))

    return make


def build_fault_report(
    scenario: ScenarioId,
    seed: int,
    ticks: int,
    baseline_seeds: Sequence[int],
) -> tuple[Investigation, Mapping[str, TelemetryEvent]] | None:
    """Build the report for the first incident of one simulated fault run.

    Returns the report and the source run's events by ID, or None when the fault
    run produces no incident. Baselines are checked by the offline synthetic
    validation before they are trusted, as in the evaluation script.
    """
    baseline_specs = [
        SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=item, ticks=ticks)
        for item in baseline_seeds
    ]
    baselines = [run_simulation(spec) for spec in baseline_specs]
    trusted = TrustedBaselines(
        tuple(validate_synthetic_baseline(run) for run in baselines)
    )
    fault = run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=ticks))

    runs = [
        ObservedRun(
            run_id=run.run_id,
            tick_interval_seconds=run.spec.tick_interval_seconds,
            events=run.events,
        )
        for run in [*baselines, fault]
    ]
    baseline_ids = [run.run_id for run in baselines]
    incidents = [
        incident
        for incident in build_incidents(runs, baseline_ids, trusted=trusted)
        if incident.source_run_id == fault.run_id
    ]
    if not incidents:
        return None

    report = investigate(
        incidents[0],
        score_run(runs, baseline_ids, fault.run_id, trusted=trusted),
    )
    return report, {event.event_id: event for event in fault.events}


def run_demo(
    scenario: ScenarioId,
    seed: int,
    ticks: int,
    baseline_seeds: Sequence[int],
    *,
    limits: BudgetLimits,
    make_provider: ProviderFactory = fake_provider_factory(AnswerFormat.FULL),
    pricing: PricingTable | None = None,
    answer_format: AnswerFormat = AnswerFormat.FULL,
) -> AgentRun | None:
    """Investigate the first incident of one fault run, within ``limits``.

    The run's budget starts only after the report is built, so the deadline
    measures agent time. The same budget object is shared by the provider and the
    loop. ``answer_format`` must be the format ``make_provider`` answers in.
    """
    built = build_fault_report(scenario, seed, ticks, baseline_seeds)
    if built is None:
        return None
    report, events_by_id = built
    budget = RunBudget(limits)
    provider = make_provider(report, budget)
    return run_agent(
        report,
        events_by_id,
        provider,
        budget=budget,
        pricing=pricing,
        answer_format=answer_format,
    )


def live_provider_factory(answer_format: AnswerFormat) -> ProviderFactory:
    """Build the OpenAI live provider factory. Fails closed on any missing setting.

    The OpenAI adapter module is imported here, and only here, so nothing else
    loads the SDK. Building the client makes no request.
    """
    settings = load_live_model_settings(os.environ)
    from app.services.agent_openai_provider import build_live_provider

    def make(report: Investigation, budget: RunBudget) -> ModelProvider:
        """Build the live provider for this run, sharing its budget."""
        return build_live_provider(settings, budget, answer_format)

    return make


def anthropic_provider_factory(answer_format: AnswerFormat) -> ProviderFactory:
    """Build the Anthropic live provider factory. Fails closed on missing settings.

    The Anthropic adapter module is imported here, and only here. Building the
    client makes no request.
    """
    settings = load_live_model_settings(
        os.environ,
        key_variable=ANTHROPIC_API_KEY_VARIABLE,
    )
    from app.services.agent_anthropic_provider import build_live_provider

    def make(report: Investigation, budget: RunBudget) -> ModelProvider:
        """Build the live provider for this run, sharing its budget."""
        return build_live_provider(settings, budget, answer_format)

    return make


def limits_from_arguments(arguments: Mapping[str, Any]) -> BudgetLimits:
    """Build validated limits, keeping the default for any flag not given.

    Raises ``ValidationError`` for a value outside its bounds, so no unchecked
    budget can reach a run.
    """
    overrides = {
        name: value for name, value in arguments.items() if value is not None
    }
    return BudgetLimits(**overrides)


def _budget_error(error: ValidationError) -> str:
    """Name the flags that are out of bounds. Never echoes a value."""
    names = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
    flags = ", ".join(BUDGET_FLAGS.get(name, name) for name in names)
    return f"a budget value is outside its allowed range: {flags}"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the demo, print the agent run as JSON, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scenario",
        type=ScenarioId,
        choices=FAULT_SCENARIOS,
        default=ScenarioId.CPU_SATURATION,
    )
    parser.add_argument("--seed", type=int, default=601)
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument(
        "--baseline-seeds",
        type=int,
        nargs="+",
        default=list(DEFAULT_BASELINE_SEEDS),
    )
    parser.add_argument(
        "--provider",
        choices=("fake", "openai", "anthropic"),
        default="fake",
        help="fake is the default. openai and anthropic also need --live.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="allow the openai provider to make paid requests.",
    )
    parser.add_argument(
        "--answer-format",
        type=AnswerFormat,
        choices=tuple(AnswerFormat),
        default=AnswerFormat.FULL,
        help="full is the default. compact names records by ID only.",
    )
    parser.add_argument("--pricing-file", type=Path, default=None)
    parser.add_argument("--max-requests", type=int, default=None)
    parser.add_argument("--max-tool-calls", type=int, default=None)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--request-timeout", type=float, default=None)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    args = parser.parse_args(argv)

    if args.provider in LIVE_PROVIDERS and not args.live:
        print(
            f"the {args.provider} provider makes paid requests; add --live",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION
    if args.live and args.provider not in LIVE_PROVIDERS:
        print("--live applies only to a live provider", file=sys.stderr)
        return EXIT_CONFIGURATION

    try:
        limits = limits_from_arguments(
            {
                "max_requests": args.max_requests,
                "max_tool_calls": args.max_tool_calls,
                "max_total_tokens": args.max_total_tokens,
                "max_output_tokens": args.max_output_tokens,
                "request_timeout_seconds": args.request_timeout,
                "deadline_seconds": args.max_seconds,
                "max_retries": args.max_retries,
            }
        )
    except ValidationError as error:
        print(_budget_error(error), file=sys.stderr)
        return EXIT_CONFIGURATION

    pricing: PricingTable | None = None
    if args.pricing_file is not None:
        try:
            pricing = load_pricing(args.pricing_file)
        except PricingError as error:
            print(str(error), file=sys.stderr)
            return EXIT_CONFIGURATION

    make_provider = fake_provider_factory(args.answer_format)
    if args.provider in LIVE_PROVIDERS:
        try:
            make_provider = (
                live_provider_factory(args.answer_format)
                if args.provider == "openai"
                else anthropic_provider_factory(args.answer_format)
            )
        except ConfigurationError as error:
            print(str(error), file=sys.stderr)
            return EXIT_CONFIGURATION

    run = run_demo(
        args.scenario,
        args.seed,
        args.ticks,
        args.baseline_seeds,
        limits=limits,
        make_provider=make_provider,
        pricing=pricing,
        answer_format=args.answer_format,
    )
    if run is None:
        print("no incident was detected for this fault run", file=sys.stderr)
        return EXIT_NO_INCIDENT
    print(run.model_dump_json(indent=2))
    if run.status is AgentStatus.VERIFIED:
        return EXIT_VERIFIED
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
