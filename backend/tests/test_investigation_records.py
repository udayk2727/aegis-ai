"""Validation tests for the investigation contracts.

These models are the guarantee that an unsupported claim cannot be represented.
Each invalid case below would otherwise let a hypothesis, a contradiction, or a
check point at nothing, or let a hypothesis read as verified.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.incident_records import RootCauseAssessment
from app.domain.investigation_records import (
    CheckKind,
    ContradictoryEvidence,
    Hypothesis,
    HypothesisKind,
    HypothesisRole,
    HypothesisStatus,
    Investigation,
    InvestigationStatus,
    ObservationKind,
    SeriesRef,
    SuggestedCheck,
    VerificationStatus,
    VerifiedObservation,
)
from app.domain.telemetry import MetricName

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
DB = SeriesRef(service="orders-db", metric_name=MetricName.DB_QUERY_LATENCY_MS)
LATENCY = SeriesRef(
    service="orders-service", metric_name=MetricName.REQUEST_LATENCY_MS
)


def _anomalous(event_ids: tuple[str, ...] = ("evt-a",)) -> VerifiedObservation:
    return VerifiedObservation(
        observation_id="obs-anomalous_series-orders-db-db_query_latency_ms",
        kind=ObservationKind.ANOMALOUS_SERIES,
        series=(DB,),
        statement="db query latency was anomalous at 1 alert tick(s).",
        source_event_ids=event_ids,
        source_event_count=len(event_ids),
        first_at=T0,
        last_at=T0,
    )


def _hypothesis(
    *,
    observation_ids: tuple[str, ...],
    event_ids: tuple[str, ...],
    contradicting: tuple[str, ...] = (),
    status: HypothesisStatus = HypothesisStatus.SUPPORTED,
) -> Hypothesis:
    return Hypothesis(
        kind=HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION,
        rank=1,
        role=HypothesisRole.POTENTIAL_INITIATING_FAULT,
        statement="consistent with slower queries.",
        status=status,
        verification_status=VerificationStatus.UNVERIFIED,
        affected_services=("orders-db",),
        supporting_observation_ids=observation_ids,
        contradicting_observation_ids=contradicting,
        supporting_event_ids=event_ids,
        supporting_event_count=len(event_ids),
        alternative_kinds=(),
        uncertainty="query traces are not stored.",
    )


def _investigation(**overrides: object) -> Investigation:
    fields: dict[str, object] = {
        "investigation_id": "inv-0123456789abcdef",
        "analysis_version": "test",
        "source_incident_id": "inc-0123456789abcdef",
        "source_run_id": "sim-0123456789abcdef",
        "baseline_run_ids": ("sim-fedcba9876543210",),
        "analysis_span_start": T0,
        "analysis_span_end": T1,
        "status": InvestigationStatus.ANALYZED,
        "root_cause_assessment": RootCauseAssessment.NOT_ASSESSED,
        "ranking_basis": "ordering aid only.",
        "ranking_caveat": "ordering aid only.",
        "verified_observations": (_anomalous(),),
        "hypotheses": (
            _hypothesis(
                observation_ids=("obs-anomalous_series-orders-db-db_query_latency_ms",),
                event_ids=("evt-a",),
            ),
        ),
        "contradictory_evidence": (),
        "missing_evidence": (),
        "suggested_checks": (),
        "limitations": ("not a causal finding.",),
    }
    fields.update(overrides)
    return Investigation.model_validate(fields)


def test_valid_observation_round_trips() -> None:
    observation = _anomalous()

    assert VerifiedObservation.model_validate(observation.model_dump()) == observation


def test_single_series_observation_cannot_claim_two_series() -> None:
    with pytest.raises(ValidationError, match="names 1 series"):
        VerifiedObservation(
            observation_id="obs-anomalous_series-orders-db-db_query_latency_ms",
            kind=ObservationKind.ANOMALOUS_SERIES,
            series=(DB, LATENCY),
            statement="x",
            source_event_ids=("evt-a",),
            source_event_count=1,
            first_at=T0,
            last_at=T0,
        )


def test_onset_observation_needs_two_series() -> None:
    with pytest.raises(ValidationError, match="names 2 series"):
        VerifiedObservation(
            observation_id="obs-onset_order-orders-db-db_query_latency_ms",
            kind=ObservationKind.ONSET_ORDER,
            series=(DB,),
            statement="x",
            source_event_ids=("evt-a", "evt-b"),
            source_event_count=2,
            first_at=T0,
            last_at=T1,
        )


def test_observation_without_source_events_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _anomalous(event_ids=())


def test_observation_end_before_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not be earlier"):
        VerifiedObservation(
            observation_id="obs-anomalous_series-orders-db-db_query_latency_ms",
            kind=ObservationKind.ANOMALOUS_SERIES,
            series=(DB,),
            statement="x",
            source_event_ids=("evt-a",),
            source_event_count=1,
            first_at=T1,
            last_at=T0,
        )


def test_observation_id_must_match_pattern() -> None:
    with pytest.raises(ValidationError):
        VerifiedObservation(
            observation_id="not an id",
            kind=ObservationKind.ANOMALOUS_SERIES,
            series=(DB,),
            statement="x",
            source_event_ids=("evt-a",),
            source_event_count=1,
            first_at=T0,
            last_at=T0,
        )


def test_hypothesis_is_always_unverified() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(
            {
                **_hypothesis(
                    observation_ids=(
                        "obs-anomalous_series-orders-db-db_query_latency_ms",
                    ),
                    event_ids=("evt-a",),
                ).model_dump(),
                "verification_status": "verified",
            }
        )


def test_hypothesis_status_must_reflect_contradictions() -> None:
    with pytest.raises(ValidationError, match="must reflect"):
        _hypothesis(
            observation_ids=("obs-anomalous_series-orders-db-db_query_latency_ms",),
            event_ids=("evt-a",),
            contradicting=("obs-normal_series-orders-service-request_latency_ms",),
            status=HypothesisStatus.SUPPORTED,
        )


def test_hypothesis_needs_supporting_observation() -> None:
    with pytest.raises(ValidationError):
        _hypothesis(observation_ids=(), event_ids=())


def test_valid_investigation_builds() -> None:
    report = _investigation()

    assert report.status is InvestigationStatus.ANALYZED
    assert report.root_cause_assessment is RootCauseAssessment.NOT_ASSESSED


def test_hypothesis_cannot_cite_an_unknown_observation() -> None:
    with pytest.raises(ValidationError, match="unknown observation"):
        _investigation(
            hypotheses=(
                _hypothesis(
                    observation_ids=("obs-anomalous_series-missing-metric",),
                    event_ids=("evt-a",),
                ),
            )
        )


def test_supporting_events_must_come_from_supporting_observations() -> None:
    with pytest.raises(ValidationError, match="supporting events"):
        _investigation(
            hypotheses=(
                _hypothesis(
                    observation_ids=(
                        "obs-anomalous_series-orders-db-db_query_latency_ms",
                    ),
                    event_ids=("evt-other",),
                ),
            )
        )


def test_ranks_must_run_from_one_without_gaps() -> None:
    hypothesis = _hypothesis(
        observation_ids=("obs-anomalous_series-orders-db-db_query_latency_ms",),
        event_ids=("evt-a",),
    ).model_copy(update={"rank": 2})

    with pytest.raises(ValidationError, match="ranks must run from 1"):
        _investigation(hypotheses=(hypothesis,))


def test_analyzed_status_requires_a_hypothesis() -> None:
    with pytest.raises(ValidationError, match="analyzed exactly"):
        _investigation(hypotheses=())


def test_insufficient_status_rejects_hypotheses() -> None:
    with pytest.raises(ValidationError, match="analyzed exactly"):
        _investigation(status=InvestigationStatus.INSUFFICIENT_EVIDENCE)


def test_contradiction_must_belong_to_a_listing_hypothesis() -> None:
    contradiction = ContradictoryEvidence(
        hypothesis_kind=HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION,
        observation_id="obs-anomalous_series-orders-db-db_query_latency_ms",
        explanation="not listed on the hypothesis.",
        source_event_ids=("evt-a",),
        source_event_count=1,
    )

    with pytest.raises(ValidationError, match="must belong to its hypothesis"):
        _investigation(contradictory_evidence=(contradiction,))


def test_suggested_check_is_read_only_by_construction() -> None:
    with pytest.raises(ValidationError):
        SuggestedCheck.model_validate(
            {
                "kind": CheckKind.REVIEW_QUERY_TRACES,
                "description": "restart the database.",
                "read_only": False,
                "affects": [HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION],
            }
        )


def test_investigation_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _investigation(root_cause="slow queries")
