"""Tests for the compact final answer: parsing, expansion, and verification.

The compact answer names records by ID. These tests check that trusted code
rebuilds the same references as the full answer, that every invalid selection is
refused, and that the verifier is the same one a full answer meets. Every provider
is a fake client, so nothing here makes a request or needs a credential.
"""

import json
from typing import Any

from app.domain.agent_records import (
    AgentOutput,
    AgentStatus,
    AnswerFormat,
    ClaimKind,
    CompactAgentOutput,
    FailureCode,
    ProposedCheck,
    ProviderTurn,
    RecordSource,
)
from app.domain.investigation_records import CheckKind, HypothesisKind
from app.domain.telemetry import ScenarioId
from app.services import agent_orchestration
from app.services.agent_compact import expand_compact
from app.services.agent_fake_provider import (
    ScriptedProvider,
    compact_reference_turns,
    reference_turns,
)
from app.services.agent_orchestration import run_agent
from tests.agent_fixtures import fault_fixture

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
OTHER_REPORT = fault_fixture(ScenarioId.DATABASE_LATENCY_SPIKE).report
MODEL = "claude-sonnet-5-5"
COMPACT = AnswerFormat.COMPACT
FULL = AnswerFormat.FULL
KIND_FOR_SOURCE = {
    RecordSource.OBSERVATION: ClaimKind.OBSERVATION,
    RecordSource.HYPOTHESIS: ClaimKind.HYPOTHESIS,
    RecordSource.MISSING_EVIDENCE: ClaimKind.UNCERTAINTY,
    RecordSource.LIMITATION: ClaimKind.UNCERTAINTY,
    RecordSource.RANKING_CAVEAT: ClaimKind.UNCERTAINTY,
}


def _compact(report: Any = REPORT) -> CompactAgentOutput:
    """The faithful compact answer for a report, as the reference reader gives it."""
    return CompactAgentOutput.model_validate_json(
        compact_reference_turns(report)[-1].final_text or ""
    )


def _turns(answer: CompactAgentOutput | str, report: Any = REPORT) -> Any:
    """Tool turns for the report, then one final answer given as a model or text."""
    text = answer if isinstance(answer, str) else answer.model_dump_json()
    return (*compact_reference_turns(report)[:-1], ProviderTurn(final_text=text))


def _verdict(
    answer: CompactAgentOutput | str,
    report: Any = REPORT,
    events: Any = EVENTS,
) -> Any:
    """Run the agent with a scripted compact answer and return the run."""
    provider = ScriptedProvider(_turns(answer, report))
    return run_agent(report, events, provider, answer_format=COMPACT)


def _codes(run: Any) -> set[FailureCode]:
    """The failure codes a run reports."""
    return {failure.code for failure in run.failures}


def _unsuggested_check(report: Any) -> ProposedCheck:
    """A check and hypothesis pair the report does not suggest."""
    suggested = {(item.kind, item.affects) for item in report.suggested_checks}
    for kind in CheckKind:
        for hypothesis in HypothesisKind:
            pair = (kind, (hypothesis,))
            if pair not in suggested:
                return ProposedCheck(kind=kind, affects=(hypothesis,))
    raise AssertionError("every check pair is suggested")


# ----- Parsing and expansion -----------------------------------------------------


def test_a_compact_reference_answer_is_verified() -> None:
    run = _verdict(_compact())

    assert run.status is AgentStatus.VERIFIED
    assert run.failures == ()
    assert run.claims


def test_compact_and_full_answers_yield_the_same_verified_claims() -> None:
    full = run_agent(REPORT, EVENTS, ScriptedProvider(reference_turns(REPORT)))
    compact = _verdict(_compact())

    assert compact.claims == full.claims
    assert compact.proposed_checks == full.proposed_checks


def test_expansion_takes_event_ids_from_the_report_and_never_text() -> None:
    expanded = expand_compact(_compact(), REPORT)
    observations = {item.observation_id: item for item in REPORT.verified_observations}
    observation_claims = [
        claim for claim in expanded.claims if claim.kind is ClaimKind.OBSERVATION
    ]

    assert len(observation_claims) == len(REPORT.verified_observations)
    for claim in observation_claims:
        record = observations[claim.reference.record_id]
        assert claim.reference.event_ids == record.source_event_ids
        assert claim.text is None


def test_each_source_keeps_its_own_kind_observations_hypotheses_and_unknowns() -> None:
    expanded = expand_compact(_compact(), REPORT)

    for claim in expanded.claims:
        assert claim.kind is KIND_FOR_SOURCE[claim.reference.source]
    sources = {claim.reference.source for claim in expanded.claims}
    assert {
        RecordSource.OBSERVATION,
        RecordSource.HYPOTHESIS,
        RecordSource.RANKING_CAVEAT,
    } <= sources


def test_a_compact_answer_reaches_the_same_verifier_as_a_full_one(
    monkeypatch: Any,
) -> None:
    seen: list[AgentOutput] = []
    real_verify = agent_orchestration.verify_output

    def spy(output: AgentOutput, report: Any, events: Any) -> Any:
        """Record what the verifier receives, then run the real verifier."""
        seen.append(output)
        return real_verify(output, report, events)

    monkeypatch.setattr(agent_orchestration, "verify_output", spy)

    run = _verdict(_compact())

    assert run.status is AgentStatus.VERIFIED
    assert len(seen) == 1
    assert isinstance(seen[0], AgentOutput)


# ----- Invalid selections are refused, not dropped -------------------------------


def test_an_unknown_observation_id_is_rejected_not_dropped() -> None:
    answer = _compact().model_copy(update={"observations": ("obs-not-in-report",)})

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert run.claims == ()
    assert FailureCode.UNKNOWN_REFERENCE in _codes(run)


def test_an_observation_of_another_incident_is_rejected_as_unknown() -> None:
    own_ids = {item.observation_id for item in REPORT.verified_observations}
    other_id = next(
        item.observation_id
        for item in OTHER_REPORT.verified_observations
        if item.observation_id not in own_ids
    )
    answer = _compact().model_copy(update={"observations": (other_id,)})

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert FailureCode.UNKNOWN_REFERENCE in _codes(run)


def test_a_hypothesis_the_report_did_not_generate_is_rejected() -> None:
    generated = {item.kind for item in REPORT.hypotheses}
    missing = next(kind for kind in HypothesisKind if kind not in generated)
    answer = _compact().model_copy(update={"hypotheses": (missing,)})

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert FailureCode.UNKNOWN_REFERENCE in _codes(run)


def test_an_answer_naming_another_investigation_is_rejected() -> None:
    answer = _compact().model_copy(
        update={"investigation_id": OTHER_REPORT.investigation_id}
    )

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert _codes(run) == {FailureCode.CROSS_INCIDENT_REFERENCE}


def test_a_compact_answer_for_another_incident_is_rejected() -> None:
    answer = _compact(OTHER_REPORT)

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert FailureCode.CROSS_INCIDENT_REFERENCE in _codes(run)


def test_a_hypothesis_without_the_ranking_caveat_is_rejected() -> None:
    kept = tuple(
        item
        for item in _compact().uncertainties
        if item.source is not RecordSource.RANKING_CAVEAT
    )
    answer = _compact().model_copy(update={"uncertainties": kept})

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert FailureCode.MISSING_CAVEAT in _codes(run)


def test_a_check_the_report_does_not_suggest_is_rejected() -> None:
    check = _unsuggested_check(REPORT)
    answer = _compact().model_copy(update={"proposed_checks": (check,)})

    run = _verdict(answer)

    assert run.status is AgentStatus.REJECTED
    assert FailureCode.UNSUPPORTED_CHECK in _codes(run)


def test_an_event_missing_from_the_source_run_is_rejected() -> None:
    run = _verdict(_compact(), events={})

    assert run.status is AgentStatus.REJECTED
    assert FailureCode.UNKNOWN_EVENT in _codes(run)


# ----- Malformed answers and format mismatches fail closed -----------------------


def _with(**changes: Any) -> dict[str, Any]:
    """The faithful compact answer as a dict, with some fields changed."""
    data = json.loads(_compact().model_dump_json())
    data.update(changes)
    return data


def test_truncated_json_is_malformed() -> None:
    run = _verdict('{"investigation_id": "inv-')

    assert run.status is AgentStatus.MALFORMED_OUTPUT
    assert run.claims == ()


def test_a_free_text_field_is_refused_as_an_extra_key() -> None:
    text = json.dumps(_with(text="the cpu was saturated"))

    run = _verdict(text)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_an_event_list_is_refused_as_an_extra_key() -> None:
    text = json.dumps(_with(event_ids=["evt-1"]))

    run = _verdict(text)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_an_observation_entry_that_is_not_an_id_is_malformed() -> None:
    text = json.dumps(_with(observations=["not an observation id"]))

    run = _verdict(text)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_an_uncertainty_citing_an_observation_is_malformed() -> None:
    entry = {"source": "observation", "record_id": "obs-x"}
    text = json.dumps(_with(uncertainties=[entry]))

    run = _verdict(text)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_more_references_than_the_answer_may_carry_is_malformed() -> None:
    observation_id = REPORT.verified_observations[0].observation_id
    text = json.dumps(_with(observations=[observation_id] * 257))

    run = _verdict(text)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_a_compact_answer_under_the_full_format_is_malformed() -> None:
    provider = ScriptedProvider(_turns(_compact()))

    run = run_agent(REPORT, EVENTS, provider, answer_format=FULL)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_a_full_answer_under_the_compact_format_is_malformed() -> None:
    provider = ScriptedProvider(reference_turns(REPORT))

    run = run_agent(REPORT, EVENTS, provider, answer_format=COMPACT)

    assert run.status is AgentStatus.MALFORMED_OUTPUT


def test_the_compact_answer_is_smaller_than_the_full_answer_without_text() -> None:
    full = json.loads(reference_turns(REPORT)[-1].final_text or "")
    for claim in full["claims"]:
        claim["text"] = None

    assert len(_compact().model_dump_json()) < len(json.dumps(full))
