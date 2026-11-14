"""Tests for run budgets: limits, request and time exhaustion, retries, and pricing.

All clocks are injected, and every provider is a fake client, so no test waits
for real time or makes a request.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.domain.agent_records import (
    AgentStatus,
    BudgetLimits,
    BudgetStop,
    TokenUsage,
)
from app.domain.telemetry import ScenarioId
from app.services.agent_budget import (
    ModelPrice,
    PricingError,
    PricingTable,
    RunBudget,
    build_accounting,
    estimate_cost,
    estimate_tokens,
    load_pricing,
)
from tests.agent_fixtures import fault_fixture

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
REQUEST = httpx.Request("POST", "https://api.invalid/v1/responses")


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        """The current time."""
        return self.now


def test_defaults_are_conservative_and_retries_are_off() -> None:
    limits = BudgetLimits()

    assert limits.max_requests == 8
    assert limits.max_tool_calls == 6
    assert limits.max_total_tokens == 30_000
    assert limits.max_output_tokens == 1_000
    assert limits.request_timeout_seconds == 30.0
    assert limits.deadline_seconds == 180.0
    assert limits.max_retries == 0


@pytest.mark.parametrize(
    "values",
    [
        {"max_requests": 0},
        {"max_requests": 21},
        {"max_tool_calls": -1},
        {"max_total_tokens": 0},
        {"max_total_tokens": 200_001},
        {"max_output_tokens": 4_001},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": 121},
        {"deadline_seconds": 901},
        {"max_retries": 4},
        {"unknown_field": 1},
    ],
)
def test_limits_outside_their_bounds_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BudgetLimits(**values)


def test_the_request_limit_stops_the_next_request_once_reached() -> None:
    budget = RunBudget(BudgetLimits(max_requests=1), clock=Clock())

    assert budget.admit_request(estimate_tokens("x")) is None
    budget.record_request()

    assert budget.admit_request(estimate_tokens("x")) is BudgetStop.REQUESTS
    assert budget.requests == 1


def test_a_request_is_refused_when_its_reserve_would_pass_the_token_limit() -> None:
    budget = RunBudget(BudgetLimits(max_total_tokens=2_000, max_output_tokens=1_000))

    stop = budget.admit_request(estimated_input_tokens=1_500)

    assert stop is BudgetStop.TOKENS
    assert budget.requests == 0


def test_a_request_fits_when_its_reserve_is_within_the_token_limit() -> None:
    budget = RunBudget(BudgetLimits(max_total_tokens=3_000, max_output_tokens=1_000))

    assert budget.admit_request(estimated_input_tokens=1_500) is None


def test_no_request_is_admitted_once_the_deadline_has_passed() -> None:
    clock = Clock()
    budget = RunBudget(BudgetLimits(deadline_seconds=60), clock=clock)

    clock.now = 61.0

    assert budget.admit_request(estimated_input_tokens=1) is BudgetStop.DEADLINE
    assert budget.requests == 0


def test_a_reply_that_arrives_after_the_deadline_is_not_acted_on() -> None:
    clock = Clock()
    budget = RunBudget(BudgetLimits(deadline_seconds=60), clock=clock)

    clock.now = 61.0

    assert budget.check_after_reply() is BudgetStop.DEADLINE


def test_a_reply_exactly_at_the_token_limit_is_still_acted_on() -> None:
    budget = RunBudget(BudgetLimits(max_total_tokens=100))
    budget.add_usage(TokenUsage(input_tokens=60, output_tokens=40))

    assert budget.check_after_reply() is None


def test_the_tool_limit_counts_refused_requests() -> None:
    budget = RunBudget(BudgetLimits(max_tool_calls=2))

    budget.record_tool_request()
    budget.record_tool_request()

    assert budget.tool_call_allowed() is BudgetStop.TOOL_CALLS


def test_estimates_are_conservative_and_labelled_as_estimates() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 2
    assert estimate_tokens("abcd") == 2


def test_cost_is_unknown_without_a_pricing_file_and_never_zero() -> None:
    usage = TokenUsage(input_tokens=1_000, output_tokens=500)
    cost, basis = estimate_cost(usage, "m", None)

    assert cost is None
    assert basis.startswith("unknown")


def test_cost_is_unknown_for_a_model_with_no_configured_price() -> None:
    pricing = PricingTable()

    usage = TokenUsage(input_tokens=1, output_tokens=1)
    cost, basis = estimate_cost(usage, "m", pricing)

    assert cost is None
    assert "no price" in basis


def test_cost_is_computed_from_the_configured_prices() -> None:
    pricing = PricingTable(
        models={
            "test-model": ModelPrice(
                input_usd_per_million_tokens="2.50",
                output_usd_per_million_tokens="10.00",
                source="operator pricing sheet",
                verified_on="2026-11-01",
            )
        }
    )

    cost, basis = estimate_cost(
        TokenUsage(input_tokens=1_000_000, output_tokens=100_000),
        "test-model",
        pricing,
    )

    assert cost is not None
    assert cost == pytest.approx(3.5)
    assert "operator pricing sheet" in basis
    assert "2026-11-01" in basis


def test_accounting_without_a_model_reports_unknown_cost() -> None:
    budget = RunBudget(BudgetLimits())

    accounting = build_accounting(
        budget=budget,
        final_status=AgentStatus.VERIFIED,
        model_id=None,
        completed_tool_calls=0,
        pricing=None,
    )

    assert accounting.estimated_cost_usd is None
    assert accounting.final_status is AgentStatus.VERIFIED


def test_a_pricing_file_is_validated_and_bad_rates_are_refused(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "currency": "USD",
                "models": {
                    "test-model": {
                        "input_usd_per_million_tokens": "1.00",
                        "output_usd_per_million_tokens": "2.00",
                        "source": "sheet",
                        "verified_on": "2026-11-01",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "models": {
                    "test-model": {
                        "input_usd_per_million_tokens": "-1",
                        "output_usd_per_million_tokens": "2",
                        "source": "sheet",
                        "verified_on": "2026-11-01",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert "test-model" in load_pricing(good).models
    with pytest.raises(PricingError):
        load_pricing(bad)
    with pytest.raises(PricingError):
        load_pricing(tmp_path / "missing.json")


def test_a_pricing_file_with_an_unknown_currency_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "currency.json"
    path.write_text(json.dumps({"currency": "EUR", "models": {}}), encoding="utf-8")

    with pytest.raises(PricingError):
        load_pricing(path)



def test_an_admitted_request_records_its_reserve_and_its_reported_usage() -> None:
    budget = RunBudget(BudgetLimits(max_total_tokens=5_000, max_output_tokens=800))

    assert budget.admit_request(estimated_input_tokens=40, serialized_chars=120) is None
    budget.record_request()
    budget.add_usage(TokenUsage(input_tokens=50, output_tokens=7))

    (entry,) = budget.diagnostics
    assert entry.request_number == 1
    assert entry.serialized_chars == 120
    assert entry.estimated_input_tokens == 40
    assert entry.reserved_output_tokens == 800
    assert entry.remaining_tokens_before == 5_000
    assert entry.admitted is True
    assert entry.refusal is None
    assert entry.input_tokens == 50
    assert entry.output_tokens == 7


def test_a_refused_request_is_recorded_with_its_reason_and_no_provider_counts() -> None:
    budget = RunBudget(BudgetLimits(max_total_tokens=2_000, max_output_tokens=1_000))

    assert budget.admit_request(1_500, serialized_chars=3_000) is BudgetStop.TOKENS

    (entry,) = budget.diagnostics
    assert entry.request_number == 1
    assert entry.admitted is False
    assert entry.refusal is BudgetStop.TOKENS
    assert entry.remaining_tokens_before == 2_000
    assert entry.input_tokens is None
    assert entry.output_tokens is None
    assert budget.requests == 0