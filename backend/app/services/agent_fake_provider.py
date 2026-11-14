"""Deterministic fake providers for the investigation agent.

Nothing here calls a model. ``ScriptedProvider`` replays turns it was given, so
tests can produce any tool request or answer, including malformed ones.
``reference_turns`` builds the turns of a faithful reader: it calls the tools,
then quotes the report's canonical records. The local CLI uses it, and the
verifier must accept its answer.
"""

from collections.abc import Sequence

from app.domain.agent_records import (
    AgentClaim,
    AgentOutput,
    ClaimKind,
    CompactAgentOutput,
    EvidenceReference,
    ProposedCheck,
    ProviderFailureReason,
    ProviderMessage,
    ProviderTurn,
    RecordSource,
    ToolCall,
    UncertaintyRef,
)
from app.domain.investigation_records import Investigation
from app.services.agent_provider import ProviderError
from app.services.agent_tools import UNCERTAINTY_SOURCES, canonical_records

FAKE_PROVIDER_ID = "fake-deterministic"


class ScriptedProvider:
    """Replays a fixed list of turns, in order. Every transcript it sees is kept.

    Asking for more turns than the script holds raises ``ProviderError``, which
    the loop reports as a provider failure.
    """

    def __init__(
        self,
        turns: Sequence[ProviderTurn],
        provider_id: str = FAKE_PROVIDER_ID,
    ) -> None:
        self.provider_id = provider_id
        self.transcripts: list[tuple[ProviderMessage, ...]] = []
        self._turns = tuple(turns)
        self._position = 0

    def next_turn(self, transcript: Sequence[ProviderMessage]) -> ProviderTurn:
        """Return the next scripted turn, and keep a copy of what was sent."""
        self.transcripts.append(tuple(transcript))
        if self._position >= len(self._turns):
            raise ProviderError(ProviderFailureReason.EXHAUSTED)
        turn = self._turns[self._position]
        self._position += 1
        return turn


def reference_turns(report: Investigation) -> tuple[ProviderTurn, ...]:
    """Return turns that read the report with the tools and quote it faithfully.

    The answer is built from the report's own records, so it is the reference
    the verifier must accept. It cites every observation and hypothesis, every
    uncertainty, and every suggested check.
    """
    answer = AgentOutput(
        investigation_id=report.investigation_id,
        root_cause_assessment="not_assessed",
        claims=_reference_claims(report),
        proposed_checks=_reference_checks(report),
    )
    return (*_reference_tool_turns(), ProviderTurn(final_text=answer.model_dump_json()))


def compact_reference_turns(report: Investigation) -> tuple[ProviderTurn, ...]:
    """Return turns that read the report and answer with record IDs only.

    The compact answer names every observation, hypothesis, and uncertainty the
    full reference quotes, and no text or event lists. Expanding it must give
    the same references as the full answer.
    """
    answer = CompactAgentOutput(
        investigation_id=report.investigation_id,
        root_cause_assessment="not_assessed",
        observations=tuple(
            item.observation_id for item in report.verified_observations
        ),
        hypotheses=tuple(item.kind for item in report.hypotheses),
        uncertainties=tuple(
            UncertaintyRef(source=source, record_id=record_id)
            for (source, record_id) in canonical_records(report)
            if source in UNCERTAINTY_SOURCES
        ),
        proposed_checks=_reference_checks(report),
    )
    return (*_reference_tool_turns(), ProviderTurn(final_text=answer.model_dump_json()))


def _reference_tool_turns() -> tuple[ProviderTurn, ...]:
    """The tool requests a faithful reader makes before it answers."""
    return tuple(
        ProviderTurn(tool_call=ToolCall(name=name))
        for name in (
            "list_observations",
            "list_hypotheses",
            "list_uncertainties",
            "list_suggested_checks",
        )
    )


def _reference_checks(report: Investigation) -> tuple[ProposedCheck, ...]:
    """Propose each check the report suggests, with the hypotheses it affects."""
    return tuple(
        ProposedCheck(kind=item.kind, affects=item.affects)
        for item in report.suggested_checks
    )


def _reference_claims(report: Investigation) -> tuple[AgentClaim, ...]:
    """Quote each citable record of the report exactly once."""
    claims: list[AgentClaim] = []
    for observation in report.verified_observations:
        claims.append(
            AgentClaim(
                kind=ClaimKind.OBSERVATION,
                text=observation.statement,
                reference=EvidenceReference(
                    investigation_id=report.investigation_id,
                    source=RecordSource.OBSERVATION,
                    record_id=observation.observation_id,
                    event_ids=observation.source_event_ids,
                ),
            )
        )
    for hypothesis in report.hypotheses:
        claims.append(
            AgentClaim(
                kind=ClaimKind.HYPOTHESIS,
                text=hypothesis.statement,
                reference=EvidenceReference(
                    investigation_id=report.investigation_id,
                    source=RecordSource.HYPOTHESIS,
                    record_id=hypothesis.kind.value,
                ),
            )
        )
    for (source, record_id), text in canonical_records(report).items():
        if source not in UNCERTAINTY_SOURCES:
            continue
        claims.append(
            AgentClaim(
                kind=ClaimKind.UNCERTAINTY,
                text=text,
                reference=EvidenceReference(
                    investigation_id=report.investigation_id,
                    source=source,
                    record_id=record_id,
                ),
            )
        )
    return tuple(claims)
