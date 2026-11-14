"""Tests for the run budget as the OpenAI adapter uses it.

Requests, time, and tokens are checked through the adapter with a fake client. The
module skips cleanly when the optional OpenAI SDK is not installed, and the budget
tests that need no adapter still run.
"""

import json

import httpx
import pytest

pytest.importorskip("openai", reason="the optional OpenAI SDK is not installed")

from openai import APITimeoutError  # noqa: E402

from app.domain.agent_records import (  # noqa: E402
    AgentStatus,
    BudgetLimits,
    BudgetStop,
    ProviderFailureReason,
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
from tests.test_agent_budget import Clock  # noqa: E402
from tests.test_agent_openai_provider import _answer_json  # noqa: E402

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
REQUEST = httpx.Request("POST", "https://api.invalid/v1/responses")


def _timeout() -> APITimeoutError:
    """A timeout as the SDK raises it, built from a local request."""
    return APITimeoutError(request=REQUEST)


def test_the_request_timeout_never_exceeds_the_remaining_deadline() -> None:
    clock = Clock()
    budget = RunBudget(
        BudgetLimits(deadline_seconds=180, request_timeout_seconds=30),
        clock=clock,
    )

    clock.now = 170.0

    assert budget.request_timeout() == pytest.approx(10.0)


def test_the_request_timeout_is_the_configured_value_early_in_the_run() -> None:
    budget = RunBudget(BudgetLimits(deadline_seconds=180, request_timeout_seconds=30))

    assert budget.request_timeout() == pytest.approx(30.0)


def test_no_timeout_is_given_once_the_deadline_has_passed() -> None:
    clock = Clock()
    budget = RunBudget(BudgetLimits(deadline_seconds=60), clock=clock)

    clock.now = 60.5

    assert budget.request_timeout() is None
    assert budget.stop is BudgetStop.DEADLINE


def test_the_default_adapter_makes_one_request_and_no_retry() -> None:
    api = FakeResponsesApi([_timeout(), _response([])])
    sleeps: list[float] = []
    provider = OpenAIResponsesProvider(api, model="test-model", sleep=sleeps.append)

    run = run_agent(REPORT, EVENTS, provider)

    assert run.status is AgentStatus.PROVIDER_ERROR
    assert run.provider_failure is ProviderFailureReason.TIMEOUT
    assert len(api.requests) == 1
    assert sleeps == []


def test_retries_stop_at_the_request_limit_and_are_counted() -> None:
    budget = RunBudget(BudgetLimits(max_requests=2, max_retries=3))
    api = FakeResponsesApi([_timeout()] * 10)
    provider = OpenAIResponsesProvider(
        api,
        model="test-model",
        budget=budget,
        sleep=lambda _: None,
    )

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.status is AgentStatus.BUDGET_EXHAUSTED
    assert run.budget_stop is BudgetStop.REQUESTS
    assert len(api.requests) == 2
    assert run.accounting is not None
    assert run.accounting.provider_requests == 2


def test_a_run_past_its_deadline_sends_no_request() -> None:
    clock = Clock()
    budget = RunBudget(BudgetLimits(deadline_seconds=60), clock=clock)
    api = FakeResponsesApi([_response([])])
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    clock.now = 90.0
    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.status is AgentStatus.BUDGET_EXHAUSTED
    assert run.budget_stop is BudgetStop.DEADLINE
    assert api.requests == []


def test_each_request_carries_a_timeout_no_longer_than_the_deadline() -> None:
    clock = Clock()
    budget = RunBudget(
        BudgetLimits(deadline_seconds=100, request_timeout_seconds=30),
        clock=clock,
    )
    api = FakeResponsesApi(
        [
            _response([_call("list_observations", {}, "c1")]),
            _response([_call("submit_answer", _answer_json(), "c2")]),
        ]
    )
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    clock.now = 80.0
    run_agent(REPORT, EVENTS, provider, budget=budget)

    timeouts = [request["timeout"] for request in api.requests]
    assert len(timeouts) == 2
    assert all(timeout <= 20.0 + 1e-9 for timeout in timeouts)


def test_usage_is_recorded_from_each_reply_and_summed() -> None:
    api = FakeResponsesApi(
        [
            _response([_call("list_observations", {}, "c1")], usage=(100, 20)),
            _response([_call("submit_answer", _answer_json(), "c2")], usage=(50, 10)),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.status is AgentStatus.VERIFIED
    assert run.accounting is not None
    assert run.accounting.input_tokens == 150
    assert run.accounting.output_tokens == 30
    assert run.accounting.total_tokens == 180
    assert run.accounting.provider_requests == 2
    assert run.accounting.completed_tool_calls == 1
    assert run.accounting.model_id == "test-model"


def test_usage_is_attached_to_each_request_in_a_run() -> None:
    api = FakeResponsesApi(
        [
            _response([_call("list_observations", {}, "c1")], usage=(100, 20)),
            _response([_call("submit_answer", _answer_json(), "c2")], usage=(50, 10)),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.accounting is not None
    diagnostics = run.accounting.request_diagnostics
    assert [entry.request_number for entry in diagnostics] == [1, 2]
    assert [entry.input_tokens for entry in diagnostics] == [100, 50]
    assert [entry.output_tokens for entry in diagnostics] == [20, 10]
    assert all(entry.admitted for entry in diagnostics)
    assert sum(entry.input_tokens or 0 for entry in diagnostics) == (
        run.accounting.input_tokens
    )


def test_a_refused_attempt_is_listed_but_not_counted_as_a_request() -> None:
    budget = RunBudget(BudgetLimits(max_requests=1))
    api = FakeResponsesApi([_response([_call("list_observations", {}, "c1")])])
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.budget_stop is BudgetStop.REQUESTS
    assert run.accounting is not None
    assert run.accounting.provider_requests == 1
    first, refused = run.accounting.request_diagnostics
    assert first.admitted is True
    assert refused.admitted is False
    assert refused.refusal is BudgetStop.REQUESTS
    assert refused.request_number == 2
    assert refused.input_tokens is None


def test_diagnostics_hold_counts_and_fixed_reasons_and_no_record_content() -> None:
    api = FakeResponsesApi(
        [
            _response([_call("get_observation", {"observation_id": "x"}, "c1")]),
            _response([_call("submit_answer", _answer_json(), "c2")]),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = OpenAIResponsesProvider(api, model="test-model", budget=budget)

    run = run_agent(REPORT, EVENTS, provider, budget=budget)

    assert run.accounting is not None
    dumped = run.accounting.model_dump(mode="json")
    expected_keys = {
        "request_number",
        "serialized_chars",
        "estimated_input_tokens",
        "reserved_output_tokens",
        "remaining_tokens_before",
        "admitted",
        "refusal",
        "input_tokens",
        "output_tokens",
    }
    for entry in dumped["request_diagnostics"]:
        assert set(entry) == expected_keys
    encoded = json.dumps(dumped)
    statement = REPORT.verified_observations[0].statement
    assert statement not in encoded
    assert "Review investigation" not in encoded
