"""Tests for the deterministic verifier: each rule is broken on purpose once.

The reference answer is the one a faithful reader gives, so it must pass. Each
test then changes one thing. Models are copied with ``model_copy`` rather than
rebuilt, because the point is to show that the verifier catches an answer the
model constructors would have refused.
"""

from app.domain.agent_records import (
    AgentClaim,
    AgentOutput,
    ClaimKind,
    EvidenceReference,
    FailureCode,
    RecordSource,
)
from app.domain.investigation_records import (
    HypothesisKind,
    Investigation,
    InvestigationStatus,
)
from app.domain.telemetry import ScenarioId
from app.services.agent_fake_provider import reference_turns
from app.services.agent_verification import verify_output
from tests.agent_fixtures import fault_fixture

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
FOREIGN_INVESTIGATION = fault_fixture(
    ScenarioId.DATABASE_LATENCY_SPIKE
).report.investigation_id


def _reference() -> AgentOutput:
    """The faithful answer, parsed back into the output model."""
    return AgentOutput.model_validate_json(reference_turns(REPORT)[-1].final_text or "")


def _codes(
    output: AgentOutput,
    report: Investigation = REPORT,
) -> list[FailureCode]:
    """Verify an answer and return the failure codes in order."""
    return [failure.code for failure in verify_output(output, report, EVENTS)]


def _replace_claim(
    output: AgentOutput,
    index: int,
    claim: AgentClaim,
) -> AgentOutput:
    """Return the answer with one claim swapped out."""
    claims = list(output.claims)
    claims[index] = claim
    return output.model_copy(update={"claims": tuple(claims)})


def _with_reference(
    output: AgentOutput,
    index: int,
    reference: EvidenceReference,
) -> AgentOutput:
    """Return the answer with one claim's reference replaced."""
    claim = output.claims[index]
    return _replace_claim(
        output,
        index,
        claim.model_copy(update={"reference": reference}),
    )


def _index_of(output: AgentOutput, kind: ClaimKind) -> int:
    """Index of the first claim of one kind in an answer."""
    return next(
        index for index, claim in enumerate(output.claims) if claim.kind is kind
    )


def test_the_faithful_answer_verifies() -> None:
    assert verify_output(_reference(), REPORT, EVENTS) == []


def test_fabricated_observation_id_is_rejected_at_its_claim() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.OBSERVATION)
    claim = output.claims[index]
    reference = claim.reference.model_copy(
        update={"record_id": "obs-anomalous_series-invented-x"}
    )

    failures = verify_output(
        _with_reference(output, index, reference),
        REPORT,
        EVENTS,
    )

    assert [(item.code, item.claim_index) for item in failures] == [
        (FailureCode.UNKNOWN_REFERENCE, index)
    ]


def test_fabricated_event_id_is_rejected() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.OBSERVATION)
    claim = output.claims[index]
    reference = claim.reference.model_copy(update={"event_ids": ("evt-not-stored",)})

    failures = verify_output(
        _with_reference(output, index, reference),
        REPORT,
        EVENTS,
    )

    assert [item.code for item in failures] == [FailureCode.UNKNOWN_EVENT]


def test_real_event_not_listed_by_its_observation_is_rejected() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.OBSERVATION)
    claim = output.claims[index]
    cited = next(
        item
        for item in REPORT.verified_observations
        if item.observation_id == claim.reference.record_id
    )
    unlisted = next(
        event_id for event_id in EVENTS if event_id not in cited.source_event_ids
    )
    reference = claim.reference.model_copy(update={"event_ids": (unlisted,)})

    failures = verify_output(
        _with_reference(output, index, reference),
        REPORT,
        EVENTS,
    )

    assert [item.code for item in failures] == [FailureCode.EVENT_NOT_CITED]


def test_event_citation_on_a_non_observation_record_is_rejected() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.HYPOTHESIS)
    claim = output.claims[index]
    reference = claim.reference.model_copy(update={"event_ids": tuple(EVENTS)[:1]})

    failures = verify_output(
        _with_reference(output, index, reference),
        REPORT,
        EVENTS,
    )

    assert [item.code for item in failures] == [FailureCode.EVENT_NOT_CITED]


def test_reference_to_another_incident_is_rejected_at_its_claim() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.OBSERVATION)
    claim = output.claims[index]
    reference = claim.reference.model_copy(
        update={"investigation_id": FOREIGN_INVESTIGATION}
    )

    failures = verify_output(
        _with_reference(output, index, reference),
        REPORT,
        EVENTS,
    )

    assert [(item.code, item.claim_index) for item in failures] == [
        (FailureCode.CROSS_INCIDENT_REFERENCE, index)
    ]


def test_answer_for_another_incident_is_rejected_before_any_claim() -> None:
    output = _reference().model_copy(update={"investigation_id": FOREIGN_INVESTIGATION})

    assert _codes(output) == [FailureCode.CROSS_INCIDENT_REFERENCE]


def test_novel_sentence_appended_to_a_canonical_text_is_rejected() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.OBSERVATION)
    claim = output.claims[index]
    novel = claim.model_copy(
        update={"text": f"{claim.text} The root cause is confirmed."}
    )

    failures = verify_output(_replace_claim(output, index, novel), REPORT, EVENTS)

    assert [(item.code, item.claim_index) for item in failures] == [
        (FailureCode.TEXT_NOT_CANONICAL, index)
    ]


def test_claim_quoting_another_records_text_is_rejected() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.HYPOTHESIS)
    claim = output.claims[index]
    mismatched = claim.model_copy(
        update={"text": REPORT.verified_observations[0].statement}
    )

    failures = verify_output(_replace_claim(output, index, mismatched), REPORT, EVENTS)

    assert [item.code for item in failures] == [FailureCode.TEXT_NOT_CANONICAL]


def test_claim_kind_that_does_not_match_its_record_is_rejected() -> None:
    output = _reference()
    index = _index_of(output, ClaimKind.OBSERVATION)
    claim = output.claims[index]
    relabelled = claim.model_copy(update={"kind": ClaimKind.UNCERTAINTY})

    failures = verify_output(_replace_claim(output, index, relabelled), REPORT, EVENTS)

    assert [(item.code, item.claim_index) for item in failures] == [
        (FailureCode.KIND_MISMATCH, index)
    ]


def test_hypothesis_claimed_without_the_ranking_caveat_is_rejected() -> None:
    output = _reference()
    kept = tuple(
        claim
        for claim in output.claims
        if claim.reference.source is not RecordSource.RANKING_CAVEAT
    )

    codes = _codes(output.model_copy(update={"claims": kept}))

    assert FailureCode.MISSING_CAVEAT in codes


def test_hypothesis_claimed_for_insufficient_evidence_is_rejected() -> None:
    weakened = REPORT.model_copy(
        update={
            "status": InvestigationStatus.INSUFFICIENT_EVIDENCE,
            "hypotheses": (),
        }
    )

    codes = _codes(_reference(), report=weakened)

    assert FailureCode.INSUFFICIENT_EVIDENCE_CLAIM in codes


def test_proposed_check_the_report_does_not_suggest_is_rejected() -> None:
    output = _reference()
    proposed = output.proposed_checks[0]
    other = next(kind for kind in HypothesisKind if kind not in proposed.affects)
    unsupported = proposed.model_copy(update={"affects": (other,)})

    codes = _codes(
        output.model_copy(update={"proposed_checks": (unsupported,)}),
    )

    assert codes == [FailureCode.UNSUPPORTED_CHECK]


def test_broken_report_provenance_is_never_presented_as_verified() -> None:
    failures = verify_output(_reference(), REPORT, {})

    assert FailureCode.REPORT_PROVENANCE in [item.code for item in failures]
