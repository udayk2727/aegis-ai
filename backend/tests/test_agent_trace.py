"""The provider-turn count and the ordered lifecycle trace of one agent run.

These tests use scripted providers and the fixed fault fixture, so each run is
deterministic and makes no network call.
"""

import itertools
import json
from collections.abc import Callable, Sequence

import pytest
from pydantic import ValidationError

from app.domain.agent_records import (
    AgentOutput,
    AgentRun,
    AgentStatus,
    BudgetLimits,
    FailureCode,
    ProviderFailureReason,
    ProviderMessage,
    ProviderTurn,
    ToolCall,
    TraceEvent,
    TraceEventKind,
    VerificationFailure,
)
from app.domain.telemetry import ScenarioId
from app.services.agent_budget import RunBudget
from app.services.agent_fake_provider import reference_turns
from app.services.agent_orchestration import run_agent
from app.services.agent_provider import ProviderError
from app.services.agent_tools import REGISTRY
from app.services.agent_trace import TraceRecorder
from tests.agent_fixtures import fault_fixture

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
REFERENCE = reference_turns(REPORT)
TOOL_TURNS = REFERENCE[:-1]


class ScriptedProvider:
    """Replays its turns, then raises ``final_failure`` once they run out."""

    provider_id = "scripted-test"

    def __init__(
        self,
        turns: Sequence[ProviderTurn],
        final_failure: ProviderFailureReason = ProviderFailureReason.EXHAUSTED,
    ) -> None:
        self._turns = tuple(turns)
        self._position = 0
        self._final_failure = final_failure
        self.calls = 0

    def next_turn(self, transcript: Sequence[ProviderMessage]) -> ProviderTurn:
        """Return the next scripted turn, or raise the configured failure."""
        self.calls += 1
        if self._position >= len(self._turns):
            raise ProviderError(self._final_failure)
        turn = self._turns[self._position]
        self._position += 1
        return turn


def _kinds_and_outcomes(run_events: Sequence[TraceEvent]) -> list[tuple[str, str]]:
    """Each event as a (kind, outcome) pair, for an exact order check."""
    return [(event.kind.value, event.outcome) for event in run_events]


def _run(
    turns: Sequence[ProviderTurn],
    *,
    final_failure: ProviderFailureReason = ProviderFailureReason.EXHAUSTED,
    budget: RunBudget | None = None,
) -> AgentRun:
    """Run the agent over the fixed report with scripted turns."""
    return run_agent(
        REPORT,
        EVENTS,
        ScriptedProvider(turns, final_failure=final_failure),
        budget=budget or RunBudget(BudgetLimits()),
    )


def test_a_reference_run_counts_every_invocation_as_a_turn() -> None:
    run = _run(REFERENCE)

    assert run.status is AgentStatus.VERIFIED
    assert run.provider_turns == len(REFERENCE)
    assert run.accounting is not None
    assert run.accounting.provider_requests == 0
    assert run.accounting.request_diagnostics == ()


def test_an_invocation_that_raises_is_still_counted() -> None:
    run = _run(TOOL_TURNS[:1], final_failure=ProviderFailureReason.TIMEOUT)

    assert run.status is AgentStatus.PROVIDER_ERROR
    assert run.provider_turns == 2


def test_a_stop_before_any_invocation_is_not_counted() -> None:
    readings = iter([0.0])
    budget = RunBudget(
        BudgetLimits(deadline_seconds=10.0),
        clock=lambda: next(readings, 100.0),
    )

    run = _run(REFERENCE, budget=budget)

    assert run.status is AgentStatus.BUDGET_EXHAUSTED
    assert run.provider_turns == 0
    assert _kinds_and_outcomes(run.trace) == [
        ("budget_stop", "deadline"),
        ("terminal", "budget_exhausted"),
    ]


def test_a_mid_run_provider_failure_has_the_exact_expected_order() -> None:
    first_tool = TOOL_TURNS[0].tool_call
    assert first_tool is not None

    run = _run(TOOL_TURNS[:1], final_failure=ProviderFailureReason.TIMEOUT)

    assert _kinds_and_outcomes(run.trace) == [
        ("provider_turn", "invoked"),
        ("tool_outcome", "ok"),
        ("provider_turn", "invoked"),
        ("provider_failure", "timeout"),
        ("terminal", "provider_error"),
    ]
    assert [event.sequence for event in run.trace] == [1, 2, 3, 4, 5]
    assert run.trace[1].tool == first_tool.name
    assert run.tool_successes == 1
    assert run.claims == ()


def test_each_run_ends_with_exactly_one_terminal_event_last() -> None:
    runs = [
        _run(REFERENCE),
        _run(TOOL_TURNS[:1], final_failure=ProviderFailureReason.TIMEOUT),
        _run(REFERENCE, budget=RunBudget(BudgetLimits(max_tool_calls=0))),
    ]

    for run in runs:
        terminals = [e for e in run.trace if e.kind is TraceEventKind.TERMINAL]
        assert len(terminals) == 1
        assert run.trace[-1] is terminals[0]
        assert terminals[0].outcome == run.status.value


def test_a_tool_budget_stop_is_recorded_before_the_terminal_event() -> None:
    run = _run(REFERENCE, budget=RunBudget(BudgetLimits(max_tool_calls=0)))

    assert _kinds_and_outcomes(run.trace) == [
        ("provider_turn", "invoked"),
        ("budget_stop", "tool_calls"),
        ("terminal", "budget_exhausted"),
    ]


def test_an_unknown_tool_is_refused_and_its_name_never_appears() -> None:
    run = _run(
        [ProviderTurn(tool_call=ToolCall(name="shell_run_unlisted_name"))]
        + list(REFERENCE)
    )

    refusal = run.trace[1]
    assert refusal.kind is TraceEventKind.TOOL_OUTCOME
    assert refusal.outcome == "refused_unknown_tool"
    assert refusal.tool is None
    serialized = json.dumps([event.model_dump(mode="json") for event in run.trace])
    assert "shell_run_unlisted_name" not in serialized


def test_invalid_arguments_name_the_tool_but_not_the_arguments() -> None:
    run = _run(
        [
            ProviderTurn(
                tool_call=ToolCall(
                    name="list_hypotheses",
                    arguments={"url": "http://example.invalid/leak"},
                )
            ),
            *REFERENCE,
        ]
    )

    refusal = run.trace[1]
    assert refusal.outcome == "refused_invalid_arguments"
    assert refusal.tool == "list_hypotheses"
    serialized = json.dumps([event.model_dump(mode="json") for event in run.trace])
    assert "example.invalid" not in serialized


def test_trace_tool_names_are_always_from_the_allowlist() -> None:
    run = _run(REFERENCE)

    named = {event.tool for event in run.trace if event.tool is not None}
    assert named
    assert named <= set(REGISTRY)


def test_the_trace_is_excluded_from_default_serialization() -> None:
    run = _run(REFERENCE)

    dumped = json.loads(run.model_dump_json())

    assert "trace" not in dumped
    assert dumped["provider_turns"] == len(REFERENCE)
    assert len(run.trace) > 0


def test_without_a_trace_clock_events_carry_no_elapsed_time() -> None:
    run = _run(REFERENCE)

    assert all(event.elapsed_ms is None for event in run.trace)


def test_a_trace_clock_orders_elapsed_time_from_its_first_reading() -> None:
    readings = itertools.count(50.0, 0.25)
    run = run_agent(
        REPORT,
        EVENTS,
        ScriptedProvider(REFERENCE),
        budget=RunBudget(BudgetLimits()),
        trace_clock=lambda: next(readings),
    )

    elapsed = [event.elapsed_ms for event in run.trace]
    assert elapsed[0] == 0
    assert all(value is not None for value in elapsed)
    assert elapsed == sorted(elapsed)


def _budget_clock_reads(trace_clock: Callable[[], float] | None) -> int:
    """How many times the budget's clock is read by a complete reference run."""
    reads = 0

    def budget_clock() -> float:
        nonlocal reads
        reads += 1
        return 0.0

    run_agent(
        REPORT,
        EVENTS,
        ScriptedProvider(REFERENCE),
        budget=RunBudget(BudgetLimits(), clock=budget_clock),
        trace_clock=trace_clock,
    )
    return reads


def test_a_trace_clock_never_changes_how_often_the_budget_clock_is_read() -> None:
    ticks = itertools.count(0.0, 1.0)

    with_trace_clock = _budget_clock_reads(lambda: next(ticks))
    without_trace_clock = _budget_clock_reads(None)

    assert with_trace_clock == without_trace_clock


def test_a_verification_event_keeps_each_failure_code_once() -> None:
    recorder = TraceRecorder()
    failures = [
        VerificationFailure(code=FailureCode.UNKNOWN_REFERENCE, message="x"),
        VerificationFailure(
            code=FailureCode.UNKNOWN_REFERENCE,
            message="x",
            claim_index=1,
        ),
        VerificationFailure(code=FailureCode.TEXT_NOT_CANONICAL, message="y"),
    ]

    recorder.verification("rejected", failures)

    (event,) = recorder.events
    assert event.failure_codes == (
        FailureCode.UNKNOWN_REFERENCE,
        FailureCode.TEXT_NOT_CANONICAL,
    )


def test_a_rejected_answer_records_its_failure_codes() -> None:
    answer = REFERENCE[-1].final_text
    assert answer is not None
    output = AgentOutput.model_validate_json(answer)
    first = output.claims[0]
    invented = first.reference.model_copy(
        update={"record_id": "obs-anomalous_series-invented-x"}
    )
    changed = first.model_copy(update={"reference": invented})
    corrupted = output.model_copy(
        update={"claims": (changed, *output.claims[1:])}
    ).model_dump_json()

    run = _run([*TOOL_TURNS, ProviderTurn(final_text=corrupted)])

    assert run.status is AgentStatus.REJECTED
    verification = [e for e in run.trace if e.kind is TraceEventKind.VERIFICATION]
    assert [e.outcome for e in verification] == ["rejected"]
    assert verification[0].failure_codes


def test_an_event_kind_rejects_an_outcome_from_another_kind() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(
            sequence=1,
            kind=TraceEventKind.PROVIDER_TURN,
            outcome="timeout",
            elapsed_ms=None,
        )


def test_only_a_tool_outcome_may_name_a_tool() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(
            sequence=1,
            kind=TraceEventKind.PROVIDER_TURN,
            outcome="invoked",
            elapsed_ms=None,
            tool="list_observations",
        )


def test_an_unknown_tool_outcome_may_not_name_a_tool() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(
            sequence=1,
            kind=TraceEventKind.TOOL_OUTCOME,
            outcome="refused_unknown_tool",
            elapsed_ms=None,
            tool="list_observations",
        )


def test_failure_codes_belong_only_to_a_verification_event() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(
            sequence=1,
            kind=TraceEventKind.TERMINAL,
            outcome="rejected",
            elapsed_ms=None,
            failure_codes=(FailureCode.UNKNOWN_REFERENCE,),
        )
