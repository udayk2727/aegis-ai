"""Admission events in the lifecycle trace, through the transport with a fake client.

The OpenAI adapter sends each request through the transport, which records an
admission decision before it sends. These tests script the fake API, so no request
leaves the process. The module skips cleanly when the optional OpenAI SDK is not
installed.
"""

import httpx
import pytest

pytest.importorskip("openai", reason="the optional OpenAI SDK is not installed")

from openai import APITimeoutError  # noqa: E402

from app.domain.agent_records import (  # noqa: E402
    AgentStatus,
    BudgetLimits,
    BudgetStop,
    TraceEvent,
    TraceEventKind,
)
from app.domain.telemetry import ScenarioId  # noqa: E402
from app.services.agent_budget import RunBudget  # noqa: E402
from app.services.agent_openai_provider import OpenAIResponsesProvider  # noqa: E402
from app.services.agent_orchestration import run_agent  # noqa: E402
from tests.agent_fixtures import fault_fixture  # noqa: E402
from tests.agent_provider_doubles import (  # noqa: E402
    FakeResponsesApi,
    _call,
    _response,
)
from tests.test_agent_openai_provider import _answer_json  # noqa: E402

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
REQUEST = httpx.Request("POST", "https://api.invalid/v1/responses")


def _pairs(trace: tuple[TraceEvent, ...]) -> list[tuple[str, str]]:
    """Each event as a (kind, outcome) pair, for an exact order check."""
    return [(event.kind.value, event.outcome) for event in trace]


def test_a_refused_request_after_an_admitted_one_is_traced_in_order() -> None:
    budget = RunBudget(BudgetLimits(max_requests=1))
    api = FakeResponsesApi([_response([_call("list_observations", {}, "c1")])])
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.status is AgentStatus.BUDGET_EXHAUSTED
    assert run.budget_stop is BudgetStop.REQUESTS
    assert _pairs(run.trace) == [
        ("provider_turn", "invoked"),
        ("admission", "admitted"),
        ("tool_outcome", "ok"),
        ("provider_turn", "invoked"),
        ("admission", "requests"),
        ("budget_stop", "requests"),
        ("terminal", "budget_exhausted"),
    ]
    assert run.accounting is not None
    assert len(run.accounting.request_diagnostics) == 2


def test_a_timeout_after_an_admitted_request_is_traced_before_the_terminal() -> None:
    api = FakeResponsesApi(
        [
            _response([_call("list_observations", {}, "c1")]),
            APITimeoutError(request=REQUEST),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.status is AgentStatus.PROVIDER_ERROR
    assert _pairs(run.trace) == [
        ("provider_turn", "invoked"),
        ("admission", "admitted"),
        ("tool_outcome", "ok"),
        ("provider_turn", "invoked"),
        ("admission", "admitted"),
        ("provider_failure", "timeout"),
        ("terminal", "provider_error"),
    ]
    assert run.accounting is not None
    admitted = [entry.admitted for entry in run.accounting.request_diagnostics]
    assert admitted == [True, True]
    assert run.provider_turns == 2
    assert [event.sequence for event in run.trace] == list(range(1, 8))


def test_a_successful_run_traces_one_admission_per_request() -> None:
    api = FakeResponsesApi(
        [
            _response([_call("list_observations", {}, "c1")]),
            _response([_call("submit_answer", _answer_json(), "c2")]),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.status is AgentStatus.VERIFIED
    admissions = [e for e in run.trace if e.kind is TraceEventKind.ADMISSION]
    assert len(admissions) == len(run.accounting.request_diagnostics) == 2
    assert all(event.outcome == "admitted" for event in admissions)
    assert _pairs(run.trace)[-1] == ("terminal", "verified")
