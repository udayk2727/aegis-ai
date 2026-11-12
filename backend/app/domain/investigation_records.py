"""Investigation records: verified observations, hypotheses, and gaps for one incident.

An investigation separates three things. A verified observation is a fact
computed from stored events. A hypothesis is a candidate explanation, and it is
always ``unverified``. Missing evidence is what the analysis could not check.
Nothing here names a root cause, so a hypothesis cannot be read as a proven
cause, and ``root_cause_assessment`` stays ``not_assessed``.

Every statement points at stored event IDs or at other records in the same
report. The ``Investigation`` validator rejects a report whose references do not
resolve, so a claim without support cannot be represented.

Cited event lists may be bounded. Each bounded list keeps its total count next
to the listed IDs, so a reader can see how many events were cited and how many
were included. The listed IDs are always real references, and the truncation
flags are derived from the counts rather than stored separately.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.domain.incident_records import IncidentId, RootCauseAssessment
from app.domain.telemetry import (
    EventId,
    MetricName,
    RunId,
    ServiceName,
    UtcTimestamp,
)

InvestigationId = Annotated[str, StringConstraints(pattern=r"^inv-[0-9a-f]{16}$")]
ObservationId = Annotated[
    str, StringConstraints(pattern=r"^obs-[a-z0-9_.-]{1,200}$")
]


class InvestigationStatus(StrEnum):
    """``analyzed`` when at least one hypothesis was generated, else insufficient."""

    ANALYZED = "analyzed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ObservationKind(StrEnum):
    """What a verified observation states. Each is computed from stored events."""

    ANOMALOUS_SERIES = "anomalous_series"
    NORMAL_SERIES = "normal_series"
    ONSET_ORDER = "onset_order"


class HypothesisKind(StrEnum):
    """Candidate explanations the rule catalog can generate.

    Each value names a pattern in the observations, not a confirmed mechanism.
    """

    DATABASE_QUERY_PERFORMANCE_DEGRADATION = "database_query_performance_degradation"
    CPU_RESOURCE_SATURATION = "cpu_resource_saturation"
    APPLICATION_PROCESSING_BOTTLENECK = "application_processing_bottleneck"
    API_ERROR_PROPAGATION = "api_error_propagation"
    DATABASE_CONNECTIVITY_PROBLEM = "database_connectivity_problem"


class HypothesisRole(StrEnum):
    """What a hypothesis would mean if its pattern were confirmed.

    ``downstream_symptom`` marks an effect that a fault elsewhere would produce,
    such as errors passed up a call chain. ``potential_initiating_fault`` marks a
    condition that could start the degradation. The role is fixed by the rule
    catalog. It describes the hypothesis and does not change the ranking.
    """

    POTENTIAL_INITIATING_FAULT = "potential_initiating_fault"
    DOWNSTREAM_SYMPTOM = "downstream_symptom"


class HypothesisStatus(StrEnum):
    """Whether a hypothesis has contradicting observations as well as support."""

    SUPPORTED = "supported"
    SUPPORTED_WITH_CONTRADICTIONS = "supported_with_contradictions"


class VerificationStatus(StrEnum):
    """Whether a hypothesis has been verified. Always unverified today."""

    UNVERIFIED = "unverified"


def _check_event_counts(listed: int, total: int, truncated: bool) -> None:
    """Keep a cited list's total and truncation flag consistent with the listed IDs."""
    if total < listed:
        raise ValueError("event count cannot be below the listed events")
    if truncated != (total > listed):
        raise ValueError("truncation flag must match the event counts")


class MissingEvidenceKind(StrEnum):
    """Why a check could not be made, or what a hypothesis lacks."""

    NOT_IN_TELEMETRY_MODEL = "not_in_telemetry_model"
    PRIMARY_SIGNAL_NOT_SUSTAINED = "primary_signal_not_sustained"
    COMPETING_SIGNAL_NOT_RULED_OUT = "competing_signal_not_ruled_out"
    CORROBORATION_NOT_OBSERVED = "corroboration_not_observed"
    NO_MEASUREMENT_IN_RUN = "no_measurement_in_run"
    UNSCORED_MEASUREMENTS = "unscored_measurements"
    DIRECTION_NOT_EVALUATED = "direction_not_evaluated"


class CheckKind(StrEnum):
    """Read-only follow-up checks. Each one only reads or re-scores existing data."""

    REVIEW_QUERY_TRACES = "review_query_traces"
    REVIEW_APPLICATION_LOGS = "review_application_logs"
    REVIEW_CHANGE_RECORDS = "review_change_records"
    REVIEW_CONNECTION_POOL_LIMITS = "review_connection_pool_limits"
    REVIEW_HOST_METRICS = "review_host_metrics"
    RESCORE_DOWNWARD_DEVIATIONS = "rescore_downward_deviations"
    REVIEW_UNSCORED_MEASUREMENTS = "review_unscored_measurements"


class SeriesRef(BaseModel):
    """One series named by service and metric. The unit follows from the metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceName
    metric_name: MetricName


class VerifiedObservation(BaseModel):
    """A fact read from stored events, with the event IDs it was computed from.

    ``first_at`` and ``last_at`` bound the events cited. A two-series observation
    of kind ``onset_order`` covers both series, and its times span both onsets.
    ``source_event_count`` is the total cited, and ``source_event_ids`` may list
    only the first of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: ObservationId
    kind: ObservationKind
    series: tuple[SeriesRef, ...] = Field(min_length=1, max_length=2)
    statement: str = Field(min_length=1)
    source_event_ids: tuple[EventId, ...] = Field(min_length=1)
    source_event_count: Annotated[int, Field(ge=1)]
    first_at: UtcTimestamp
    last_at: UtcTimestamp

    source_events_truncated: bool = False

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        """Require one series for single-series facts and two for onset order."""
        expected = 2 if self.kind is ObservationKind.ONSET_ORDER else 1
        if len(self.series) != expected:
            raise ValueError(f"{self.kind.value} names {expected} series")
        if self.last_at < self.first_at:
            raise ValueError("last_at must not be earlier than first_at")
        _check_event_counts(
            len(self.source_event_ids),
            self.source_event_count,
            self.source_events_truncated,
        )
        return self


class Hypothesis(BaseModel):
    """A candidate explanation, ranked among the others for the same incident.

    ``rank`` orders candidates, and it is not a probability. A hypothesis is
    supported only by observations, never by its own statement. Its alternatives
    are the other hypotheses generated for the same incident. ``role`` says
    whether the pattern is an effect or a possible starting point.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: HypothesisKind
    rank: Annotated[int, Field(ge=1)]
    role: HypothesisRole
    statement: str = Field(min_length=1)
    status: HypothesisStatus
    verification_status: VerificationStatus
    affected_services: tuple[ServiceName, ...]
    supporting_observation_ids: tuple[ObservationId, ...] = Field(min_length=1)
    contradicting_observation_ids: tuple[ObservationId, ...] = ()
    supporting_event_ids: tuple[EventId, ...] = Field(min_length=1)
    supporting_event_count: Annotated[int, Field(ge=1)]
    alternative_kinds: tuple[HypothesisKind, ...]
    uncertainty: str = Field(min_length=1)

    supporting_events_truncated: bool = False

    @model_validator(mode="after")
    def _status_matches_contradictions(self) -> Self:
        """Mark a hypothesis as contradicted exactly when contradictions exist."""
        contradicted = bool(self.contradicting_observation_ids)
        flagged = self.status is HypothesisStatus.SUPPORTED_WITH_CONTRADICTIONS
        if contradicted != flagged:
            raise ValueError("status must reflect the contradicting observations")
        _check_event_counts(
            len(self.supporting_event_ids),
            self.supporting_event_count,
            self.supporting_events_truncated,
        )
        return self


class ContradictoryEvidence(BaseModel):
    """An observation a hypothesis expected but did not get, or got out of order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_kind: HypothesisKind
    observation_id: ObservationId
    explanation: str = Field(min_length=1)
    source_event_ids: tuple[EventId, ...] = Field(min_length=1)
    source_event_count: Annotated[int, Field(ge=1)]

    source_events_truncated: bool = False

    @model_validator(mode="after")
    def _count_covers_listed(self) -> Self:
        """Keep the total at or above the events actually listed."""
        _check_event_counts(
            len(self.source_event_ids),
            self.source_event_count,
            self.source_events_truncated,
        )
        return self


class MissingEvidence(BaseModel):
    """A check the analysis could not make. It names what would be needed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MissingEvidenceKind
    description: str = Field(min_length=1)
    series: tuple[SeriesRef, ...] = ()
    source_event_ids: tuple[EventId, ...] = ()
    source_event_count: Annotated[int, Field(ge=0)] = 0
    affects: tuple[HypothesisKind, ...] = Field(min_length=1)

    source_events_truncated: bool = False

    @model_validator(mode="after")
    def _count_covers_listed(self) -> Self:
        """Keep the total at or above the events actually listed."""
        _check_event_counts(
            len(self.source_event_ids),
            self.source_event_count,
            self.source_events_truncated,
        )
        return self


class SuggestedCheck(BaseModel):
    """A follow-up check that only reads or re-scores data. It never changes state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CheckKind
    description: str = Field(min_length=1)
    read_only: Literal[True] = True
    affects: tuple[HypothesisKind, ...] = Field(min_length=1)


class Investigation(BaseModel):
    """The investigation report for one incident.

    The validator checks that every reference resolves inside the report: each
    hypothesis cites observations that exist, its event IDs come from those
    observations, and each contradiction belongs to a hypothesis that lists it.
    Ranks run from 1 without gaps, and the status matches whether any hypothesis
    was generated. ``ranking_caveat`` travels with every report, so the order of
    the hypotheses is never presented without its limits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: InvestigationId
    analysis_version: str = Field(min_length=1)
    source_incident_id: IncidentId
    source_run_id: RunId
    baseline_run_ids: tuple[RunId, ...]
    analysis_span_start: UtcTimestamp
    analysis_span_end: UtcTimestamp
    status: InvestigationStatus
    root_cause_assessment: RootCauseAssessment
    ranking_basis: str = Field(min_length=1)
    ranking_caveat: str = Field(min_length=1)
    verified_observations: tuple[VerifiedObservation, ...]
    hypotheses: tuple[Hypothesis, ...]
    contradictory_evidence: tuple[ContradictoryEvidence, ...]
    missing_evidence: tuple[MissingEvidence, ...]
    suggested_checks: tuple[SuggestedCheck, ...]
    limitations: tuple[str, ...] = Field(min_length=1)

    evidence_truncated: bool = False

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        """Reject any claim whose support is missing from the report."""
        if self.analysis_span_end < self.analysis_span_start:
            raise ValueError("analysis span must not end before it starts")
        truncated_somewhere = (
            any(item.source_events_truncated for item in self.verified_observations)
            or any(item.supporting_events_truncated for item in self.hypotheses)
            or any(item.source_events_truncated for item in self.contradictory_evidence)
            or any(item.source_events_truncated for item in self.missing_evidence)
        )
        if self.evidence_truncated != truncated_somewhere:
            raise ValueError("evidence_truncated must reflect the cited lists")
        observations = {
            item.observation_id: item for item in self.verified_observations
        }
        if len(observations) != len(self.verified_observations):
            raise ValueError("observation IDs must be unique")

        kinds = [hypothesis.kind for hypothesis in self.hypotheses]
        if len(set(kinds)) != len(kinds):
            raise ValueError("each hypothesis kind may appear once")
        if [hypothesis.rank for hypothesis in self.hypotheses] != list(
            range(1, len(self.hypotheses) + 1)
        ):
            raise ValueError("hypothesis ranks must run from 1 without gaps")
        for hypothesis in self.hypotheses:
            for observation_id in (
                *hypothesis.supporting_observation_ids,
                *hypothesis.contradicting_observation_ids,
            ):
                if observation_id not in observations:
                    raise ValueError(f"unknown observation {observation_id}")
            cited = {
                event_id
                for observation_id in hypothesis.supporting_observation_ids
                for event_id in observations[observation_id].source_event_ids
            }
            if not set(hypothesis.supporting_event_ids) <= cited:
                raise ValueError(
                    "supporting events must come from supporting observations"
                )
            if not hypothesis.supporting_events_truncated and (
                set(hypothesis.supporting_event_ids) != cited
            ):
                raise ValueError(
                    "untruncated supporting events must match supporting observations"
                )

        for contradiction in self.contradictory_evidence:
            observation = observations.get(contradiction.observation_id)
            if observation is None:
                raise ValueError(f"unknown observation {contradiction.observation_id}")
            listed = any(
                hypothesis.kind is contradiction.hypothesis_kind
                and contradiction.observation_id
                in hypothesis.contradicting_observation_ids
                for hypothesis in self.hypotheses
            )
            if not listed:
                raise ValueError("a contradiction must belong to its hypothesis")
            if not set(contradiction.source_event_ids) <= set(
                observation.source_event_ids
            ):
                raise ValueError("contradiction events must come from its observation")

        generated = bool(self.hypotheses)
        analyzed = self.status is InvestigationStatus.ANALYZED
        if generated != analyzed:
            raise ValueError("status must be analyzed exactly when hypotheses exist")
        return self
