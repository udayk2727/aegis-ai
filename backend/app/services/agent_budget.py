"""Run budgets, conservative token reserves, and cost accounting for the agent.

``RunBudget`` holds the limits of one run and the usage counted so far. It is the
one place where limits are checked:

- The orchestrator checks the tool-call, token, and deadline limits, after each
  reply and before each provider call.
- The provider checks the request limit and sets the per-request timeout before
  it sends each request. A request that is refused for budget never leaves the
  process.

Token estimates are estimates, not counts. The reserve taken before a request is
one token per two characters of the serialized request, which overestimates
ordinary English text. The provider's own count is what is recorded afterwards,
and the reserve does not guarantee that a request stays within the budget.

Costs come only from a pricing file the operator supplies. No rate is built in,
and a run whose model has no configured price reports its cost as unknown.
"""

import json
import math
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.agent_records import (
    AgentStatus,
    BudgetLimits,
    BudgetStop,
    RequestDiagnostic,
    RunAccounting,
    TokenUsage,
)

MILLION = Decimal(1_000_000)
COST_QUANTUM = Decimal("0.000001")


class PricingError(ValueError):
    """The pricing file cannot be read, or it does not match the expected shape."""


class RunBudget:
    """The limits of one run, and the usage counted against them so far.

    ``requests`` counts every provider request sent, including retries and failed
    attempts. ``tool_requests`` counts every tool request, including refused ones.
    ``stop`` records the first limit that ended the run.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self.requests = 0
        self.tool_requests = 0
        self.usage = TokenUsage()
        self.stop: BudgetStop | None = None
        self.diagnostics: list[RequestDiagnostic] = []

    def elapsed(self) -> float:
        """Seconds since the run started, by the injected clock."""
        return self._clock() - self._started

    def remaining_seconds(self) -> float:
        """Seconds left before the run's deadline. Negative once it has passed."""
        return self.limits.deadline_seconds - self.elapsed()

    def request_timeout(self) -> float | None:
        """The timeout for the next request, or None if the deadline has passed.

        It never exceeds the run's deadline. A local timeout does not cancel the
        request on the server, so this bounds how long this code waits, not what
        the provider does.
        """
        remaining = self.remaining_seconds()
        if remaining <= 0:
            self._stop(BudgetStop.DEADLINE)
            return None
        return min(self.limits.request_timeout_seconds, remaining)

    def stop_before_call(self) -> BudgetStop | None:
        """Check before a provider call. A run with nothing left stops here."""
        if self.remaining_seconds() <= 0:
            return self._stop(BudgetStop.DEADLINE)
        if self.usage.total >= self.limits.max_total_tokens:
            return self._stop(BudgetStop.TOKENS)
        return None

    def check_after_reply(self) -> BudgetStop | None:
        """Check after a reply. Only an overrun stops the run.

        A reply that used the last allowed token is still acted on. A reply that
        arrives after the deadline, or that passes the token limit, is not.
        """
        if self.remaining_seconds() < 0:
            return self._stop(BudgetStop.DEADLINE)
        if self.usage.total > self.limits.max_total_tokens:
            return self._stop(BudgetStop.TOKENS)
        return None

    def admit_request(
        self,
        estimated_input_tokens: int,
        serialized_chars: int = 0,
    ) -> BudgetStop | None:
        """Check before each provider request, including each retry.

        The reserve is the estimated input plus the configured output allowance.
        A request is refused if the reserve would pass the token limit. Every
        decision is recorded in ``diagnostics``, admitted or not.
        """
        refusal = self._admission_refusal(estimated_input_tokens)
        remaining = self.limits.max_total_tokens - self.usage.total
        self.diagnostics.append(
            RequestDiagnostic(
                request_number=self.requests + 1,
                serialized_chars=serialized_chars,
                estimated_input_tokens=estimated_input_tokens,
                reserved_output_tokens=self.limits.max_output_tokens,
                remaining_tokens_before=remaining,
                admitted=refusal is None,
                refusal=refusal,
            )
        )
        if refusal is not None:
            self._stop(refusal)
        return refusal

    def _admission_refusal(self, estimated_input_tokens: int) -> BudgetStop | None:
        """The limit a request would break, or None if it may be sent."""
        if self.requests >= self.limits.max_requests:
            return BudgetStop.REQUESTS
        if self.remaining_seconds() <= 0:
            return BudgetStop.DEADLINE
        reserve = estimated_input_tokens + self.limits.max_output_tokens
        if self.usage.total + reserve > self.limits.max_total_tokens:
            return BudgetStop.TOKENS
        return None

    def tool_call_allowed(self) -> BudgetStop | None:
        """Check before a tool request is run. Refused requests also count."""
        if self.tool_requests >= self.limits.max_tool_calls:
            return self._stop(BudgetStop.TOOL_CALLS)
        return None

    def record_request(self) -> None:
        """Count one provider request that has been sent."""
        self.requests += 1

    def record_tool_request(self) -> None:
        """Count one tool request that is about to run."""
        self.tool_requests += 1

    def add_usage(self, usage: TokenUsage | None) -> None:
        """Add the tokens a reply reported. A missing report adds nothing.

        The counts also go on the latest admitted request that has none yet, so
        each diagnostic shows what its own reply reported.
        """
        self.usage = self.usage.plus(usage)
        if usage is None:
            return
        for index in range(len(self.diagnostics) - 1, -1, -1):
            entry = self.diagnostics[index]
            if entry.admitted:
                if entry.input_tokens is None:
                    self.diagnostics[index] = entry.model_copy(
                        update={
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                        }
                    )
                return

    def _stop(self, stop: BudgetStop) -> BudgetStop:
        """Record the first stop and return it."""
        if self.stop is None:
            self.stop = stop
        return stop


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: one token per two characters. Not a count."""
    return math.ceil(len(text) / 2)


def serialize_request(request: dict[str, object]) -> str:
    """The exact text a request is estimated from and measured by in diagnostics."""
    return json.dumps(request, sort_keys=True)


class ModelPrice(BaseModel):
    """Configured prices for one model, in USD per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_usd_per_million_tokens: Decimal = Field(ge=0)
    output_usd_per_million_tokens: Decimal = Field(ge=0)
    source: str = Field(min_length=1, max_length=200)
    verified_on: date


class PricingTable(BaseModel):
    """The prices an operator supplied, keyed by model identifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Literal["USD"] = "USD"
    models: dict[str, ModelPrice] = Field(default_factory=dict)


def load_pricing(path: Path) -> PricingTable:
    """Read and validate a pricing file. Any problem raises ``PricingError``."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return PricingTable.model_validate(document)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise PricingError("the pricing file cannot be read or is not valid") from error


def estimate_cost(
    usage: TokenUsage,
    model_id: str | None,
    pricing: PricingTable | None,
) -> tuple[Decimal | None, str]:
    """Return the estimated cost and how it was derived. Unknown is None, not zero."""
    if pricing is None:
        return None, "unknown: no pricing file was given"
    if model_id is None:
        return None, "unknown: this provider has no model identifier"
    price = pricing.models.get(model_id)
    if price is None:
        return None, "unknown: no price is configured for this model"
    cost = (
        Decimal(usage.input_tokens) * price.input_usd_per_million_tokens / MILLION
        + Decimal(usage.output_tokens) * price.output_usd_per_million_tokens / MILLION
    )
    basis = (
        f"estimated from the pricing file ({price.source}, "
        f"verified on {price.verified_on.isoformat()})"
    )
    return cost.quantize(COST_QUANTUM), basis


def build_accounting(
    *,
    budget: RunBudget,
    final_status: AgentStatus,
    model_id: str | None,
    completed_tool_calls: int,
    pricing: PricingTable | None,
) -> RunAccounting:
    """Assemble the usage record for one finished run."""
    cost, basis = estimate_cost(budget.usage, model_id, pricing)
    return RunAccounting(
        model_id=model_id,
        provider_requests=budget.requests,
        completed_tool_calls=completed_tool_calls,
        input_tokens=budget.usage.input_tokens,
        output_tokens=budget.usage.output_tokens,
        total_tokens=budget.usage.total,
        estimated_cost_usd=cost,
        cost_basis=basis,
        latency_seconds=budget.elapsed(),
        final_status=final_status,
        budget_stop=budget.stop,
        request_diagnostics=tuple(budget.diagnostics),
    )
