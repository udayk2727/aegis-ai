"""Deterministic verification of a model's answer against the canonical report.

The verifier accepts an answer only if every rule holds. It never removes a
failing claim and keeps the rest, because a partly checked answer would still
read as verified. A single failure rejects the whole answer, and each failure
names the claim index and a fixed reason.

The rules, in order:

- The answer and each reference name this report's investigation ID.
- Each claim's kind matches the record it cites, and the record exists.
- A claim that carries text has the record's canonical text, word for word. A
  claim without text takes that text from the record once it is accepted, so the
  server, not the model, supplies every sentence that is returned.
- Each cited event is in the source run and is listed by the cited observation.
- Each proposed check is a check the report already suggests.
- A hypothesis claim comes with the ranking caveat, and no hypothesis is claimed
  for an investigation with insufficient evidence.
- The report itself passes the provenance check, so a broken report is never
  presented as verified.

This module makes no judgement about whether a hypothesis is true. It checks
that the answer says only what the report says.
"""

from collections.abc import Mapping, Sequence

from app.domain.agent_records import (
    CLAIM_SOURCES,
    AgentClaim,
    AgentOutput,
    ClaimKind,
    FailureCode,
    ProposedCheck,
    RecordSource,
    VerificationFailure,
)
from app.domain.investigation_records import (
    Investigation,
    InvestigationStatus,
    VerifiedObservation,
)
from app.domain.telemetry import TelemetryEvent
from app.services.agent_tools import RANKING_CAVEAT_ID, canonical_records
from app.services.investigation_provenance import provenance_violations

MESSAGES: dict[FailureCode, str] = {
    FailureCode.CROSS_INCIDENT_REFERENCE: (
        "a reference names a different investigation"
    ),
    FailureCode.UNKNOWN_REFERENCE: (
        "a reference does not resolve to a record in this investigation"
    ),
    FailureCode.KIND_MISMATCH: (
        "a claim's kind does not match the record type it cites"
    ),
    FailureCode.TEXT_NOT_CANONICAL: (
        "a claim's text is not the canonical text of the record it cites"
    ),
    FailureCode.UNKNOWN_EVENT: "a cited event is not in the source run",
    FailureCode.EVENT_NOT_CITED: (
        "a cited event is not listed by the observation it is cited with"
    ),
    FailureCode.UNSUPPORTED_CHECK: (
        "a proposed check is not one the report suggests for these hypotheses"
    ),
    FailureCode.MISSING_CAVEAT: (
        "a hypothesis claim is made without the ranking caveat"
    ),
    FailureCode.INSUFFICIENT_EVIDENCE_CLAIM: (
        "a hypothesis is claimed for an investigation with insufficient evidence"
    ),
    FailureCode.REPORT_PROVENANCE: (
        "the canonical report fails its own provenance check"
    ),
}


def verify_output(
    output: AgentOutput,
    report: Investigation,
    events_by_id: Mapping[str, TelemetryEvent],
) -> list[VerificationFailure]:
    """Return every rule the answer breaks, or an empty list if all hold.

    ``events_by_id`` must hold the events of the report's source run. An empty
    list is the only result that allows the claims to be returned.
    """
    if output.investigation_id != report.investigation_id:
        return [_failure(FailureCode.CROSS_INCIDENT_REFERENCE)]

    failures: list[VerificationFailure] = []
    if provenance_violations(report, events_by_id):
        failures.append(_failure(FailureCode.REPORT_PROVENANCE))

    canonical = canonical_records(report)
    observations = {item.observation_id: item for item in report.verified_observations}
    for index, claim in enumerate(output.claims):
        failures.extend(
            _check_claim(index, claim, report, canonical, observations, events_by_id)
        )
    failures.extend(_check_conclusions(output.claims, report))
    failures.extend(_check_proposed_checks(output.proposed_checks, report))
    return failures


def hydrate_claims(
    claims: Sequence[AgentClaim],
    report: Investigation,
) -> tuple[AgentClaim, ...]:
    """Return the accepted claims with each text set to its record's canonical text.

    Call this only after ``verify_output`` returns no failures. Every reference
    has already resolved, so each lookup succeeds.
    """
    canonical = canonical_records(report)
    hydrated: list[AgentClaim] = []
    for claim in claims:
        key = (claim.reference.source, claim.reference.record_id)
        hydrated.append(claim.model_copy(update={"text": canonical[key]}))
    return tuple(hydrated)


def _failure(code: FailureCode, claim_index: int | None = None) -> VerificationFailure:
    """Build a failure with its fixed message."""
    return VerificationFailure(
        code=code,
        message=MESSAGES[code],
        claim_index=claim_index,
    )


def _check_claim(
    index: int,
    claim: AgentClaim,
    report: Investigation,
    canonical: Mapping[tuple[RecordSource, str], str],
    observations: Mapping[str, VerifiedObservation],
    events_by_id: Mapping[str, TelemetryEvent],
) -> list[VerificationFailure]:
    """Check one claim's reference, kind, text, and cited events.

    Stops at the first failure for a claim, since later checks depend on an
    earlier one having resolved.
    """
    reference = claim.reference
    if reference.investigation_id != report.investigation_id:
        return [_failure(FailureCode.CROSS_INCIDENT_REFERENCE, index)]
    if reference.source not in CLAIM_SOURCES[claim.kind]:
        return [_failure(FailureCode.KIND_MISMATCH, index)]
    key = (reference.source, reference.record_id)
    if key not in canonical:
        return [_failure(FailureCode.UNKNOWN_REFERENCE, index)]
    if claim.text is not None and claim.text != canonical[key]:
        return [_failure(FailureCode.TEXT_NOT_CANONICAL, index)]
    if not reference.event_ids:
        return []
    if reference.source is not RecordSource.OBSERVATION:
        return [_failure(FailureCode.EVENT_NOT_CITED, index)]

    listed = set(observations[reference.record_id].source_event_ids)
    failures: list[VerificationFailure] = []
    for event_id in reference.event_ids:
        if event_id not in events_by_id:
            failures.append(_failure(FailureCode.UNKNOWN_EVENT, index))
        elif event_id not in listed:
            failures.append(_failure(FailureCode.EVENT_NOT_CITED, index))
    return failures


def _check_conclusions(
    claims: tuple[AgentClaim, ...],
    report: Investigation,
) -> list[VerificationFailure]:
    """Check that the claimed conclusions fit the report's status and caveats."""
    hypothesis_index = next(
        (
            index
            for index, claim in enumerate(claims)
            if claim.kind is ClaimKind.HYPOTHESIS
        ),
        None,
    )
    if hypothesis_index is None:
        return []
    if report.status is InvestigationStatus.INSUFFICIENT_EVIDENCE:
        return [_failure(FailureCode.INSUFFICIENT_EVIDENCE_CLAIM, hypothesis_index)]
    caveated = any(
        claim.reference.source is RecordSource.RANKING_CAVEAT
        and claim.reference.record_id == RANKING_CAVEAT_ID
        for claim in claims
    )
    if not caveated:
        return [_failure(FailureCode.MISSING_CAVEAT, hypothesis_index)]
    return []


def _check_proposed_checks(
    proposed: tuple[ProposedCheck, ...],
    report: Investigation,
) -> list[VerificationFailure]:
    """Check that each proposed check is one the report already suggests."""
    suggested = {(item.kind, item.affects) for item in report.suggested_checks}
    return [
        _failure(FailureCode.UNSUPPORTED_CHECK)
        for item in proposed
        if (item.kind, item.affects) not in suggested
    ]
