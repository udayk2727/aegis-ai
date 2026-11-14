"""Expand a compact final answer into a full answer, using only trusted records.

The model names records by ID. This module turns those IDs into the same
``AgentOutput`` a full answer would be, and the existing verifier then checks it.
Nothing is verified differently, and nothing the model wrote is copied into an
evidence reference.

- An observation's events come from that observation's canonical
  ``source_event_ids``, so every reference lists exactly what the report lists.
- A hypothesis or uncertainty takes its kind from the list it appears in, so a
  claim cannot name a record of the wrong type.
- An ID the report does not hold keeps its name and an empty event list. The
  verifier then rejects it as an unknown reference, so an invalid selection is
  refused rather than dropped.
- Each reference carries the answer's investigation ID. The verifier then refuses
  an answer that names another investigation.

Text is left unset. The verifier requires no text, and the server fills each
claim's text from the canonical record once the answer is accepted.
"""

from app.domain.agent_records import (
    AgentClaim,
    AgentOutput,
    ClaimKind,
    CompactAgentOutput,
    EvidenceReference,
    RecordSource,
)
from app.domain.investigation_records import Investigation


def expand_compact(output: CompactAgentOutput, report: Investigation) -> AgentOutput:
    """Rebuild the full answer that a compact answer stands for.

    The result still goes through ``verify_output``, which decides whether it is
    accepted. Expansion itself refuses nothing, because every rule that can fail
    is one the verifier already checks.
    """
    events_by_observation = {
        item.observation_id: item.source_event_ids
        for item in report.verified_observations
    }
    claims: list[AgentClaim] = []
    for observation_id in output.observations:
        claims.append(
            _claim(
                output,
                ClaimKind.OBSERVATION,
                RecordSource.OBSERVATION,
                observation_id,
                events_by_observation.get(observation_id, ()),
            )
        )
    for kind in output.hypotheses:
        claims.append(
            _claim(output, ClaimKind.HYPOTHESIS, RecordSource.HYPOTHESIS, kind.value)
        )
    for item in output.uncertainties:
        claims.append(
            _claim(output, ClaimKind.UNCERTAINTY, item.source, item.record_id)
        )
    return AgentOutput(
        investigation_id=output.investigation_id,
        root_cause_assessment=output.root_cause_assessment,
        claims=tuple(claims),
        proposed_checks=output.proposed_checks,
    )


def _claim(
    output: CompactAgentOutput,
    kind: ClaimKind,
    source: RecordSource,
    record_id: str,
    event_ids: tuple[str, ...] = (),
) -> AgentClaim:
    """Build one claim from trusted fields only. Its text is left for the server."""
    return AgentClaim(
        kind=kind,
        reference=EvidenceReference(
            investigation_id=output.investigation_id,
            source=source,
            record_id=record_id,
            event_ids=event_ids,
        ),
    )
