"""Ordered lifecycle trace for one agent run.

The orchestrator calls this recorder at the point where each step happens, so the
order of events is the order in which the run did things. Each event is a fixed
category with a sequence number. Elapsed time is recorded only when the caller
supplies a trace clock. That clock is separate from the budget's, so tracing never
reads the clock that enforces the deadline. The recorder never sees prompts or
tool payloads and cannot store them, because its methods take only categories,
counts, and tool names from the registry.
"""

from collections.abc import Callable, Sequence
from typing import Literal

from app.domain.agent_records import (
    AgentStatus,
    BudgetStop,
    FailureCode,
    ProviderFailureReason,
    RequestDiagnostic,
    ToolResult,
    TraceEvent,
    TraceEventKind,
    VerificationFailure,
)
from app.services.agent_tools import (
    ERROR_INVALID_ARGUMENTS,
    ERROR_NO_RECORD,
    ERROR_UNKNOWN_TOOL,
)

_TOOL_REFUSALS: dict[str, str] = {
    ERROR_UNKNOWN_TOOL: "refused_unknown_tool",
    ERROR_INVALID_ARGUMENTS: "refused_invalid_arguments",
    ERROR_NO_RECORD: "refused_no_record",
}


class TraceRecorder:
    """Collects the events of one run in order.

    With no clock, events carry no timing. With a clock, the first reading is the
    origin and each event's ``elapsed_ms`` is measured from it.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock
        self._origin: float | None = None
        self._events: list[TraceEvent] = []

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """The events recorded so far, in the order they happened."""
        return tuple(self._events)

    def provider_turn(self) -> None:
        """Record one attempted provider invocation, before it is made."""
        self._emit(TraceEventKind.PROVIDER_TURN, "invoked")

    def admissions(self, diagnostics: Sequence[RequestDiagnostic]) -> None:
        """Record the transport's admission decisions made during one invocation.

        The fake provider sends no request, so it adds none. A live provider's
        requests are recorded in the same order the transport decided them.
        """
        for diagnostic in diagnostics:
            if diagnostic.refusal is None:
                outcome = "admitted"
            else:
                outcome = diagnostic.refusal.value
            self._emit(TraceEventKind.ADMISSION, outcome)

    def tool_outcome(self, result: ToolResult) -> None:
        """Record how one tool request ended, naming only allowlisted tools."""
        outcome = "ok" if result.ok else _TOOL_REFUSALS[result.error or ""]
        self._emit(TraceEventKind.TOOL_OUTCOME, outcome, tool=result.tool)

    def verification(
        self,
        outcome: Literal["verified", "rejected", "malformed_output"],
        failures: Sequence[VerificationFailure] = (),
    ) -> None:
        """Record the verifier's decision and the distinct reasons it gave.

        A faulty answer can fail many claims for the same reason, so each code is
        kept once, in the order first seen. That bounds the event by the number of
        codes, not by the number of claims.
        """
        codes = tuple(dict.fromkeys(item.code for item in failures))
        self._emit(TraceEventKind.VERIFICATION, outcome, failure_codes=codes)

    def provider_failure(self, reason: ProviderFailureReason) -> None:
        """Record a provider failure by its fixed reason. No provider text is kept."""
        self._emit(TraceEventKind.PROVIDER_FAILURE, reason.value)

    def budget_stop(self, stop: BudgetStop) -> None:
        """Record the limit that ended the run."""
        self._emit(TraceEventKind.BUDGET_STOP, stop.value)

    def terminal(self, status: AgentStatus) -> None:
        """Record the final status. Every run ends with exactly one of these."""
        self._emit(TraceEventKind.TERMINAL, status.value)

    def _elapsed_ms(self) -> int | None:
        """Milliseconds since the first reading, or None when there is no clock."""
        if self._clock is None:
            return None
        now = self._clock()
        if self._origin is None:
            self._origin = now
        return round(1000 * (now - self._origin))

    def _emit(
        self,
        kind: TraceEventKind,
        outcome: str,
        *,
        tool: str | None = None,
        failure_codes: tuple[FailureCode, ...] = (),
    ) -> None:
        """Append one event with the next sequence number and any elapsed time."""
        self._events.append(
            TraceEvent(
                sequence=len(self._events) + 1,
                kind=kind,
                outcome=outcome,
                elapsed_ms=self._elapsed_ms(),
                tool=tool,
                failure_codes=failure_codes,
            )
        )
