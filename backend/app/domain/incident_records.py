"""Incident records: validated contracts for correlated detections and evidence.

An incident records what the detection layer saw and how those alerts were
grouped. It never records a root cause. ``status`` gives the incident's place
in the detection lifecycle, and ``root_cause_assessment`` stays
``not_assessed`` until an investigation step exists, so a correlation cannot
be read as a diagnosis.

Timestamps come in two kinds, and the field names say which is which.
``*_observation_at`` is the time of a telemetry sample. ``*_detection_at`` is
the tick at which the incident rule decided to alert, using data up to and
including that tick.
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

from app.domain.telemetry import (
    EventId,
    MetricName,
    MetricValue,
    RunId,
    ServiceName,
    Severity,
    Unit,
    UtcTimestamp,
)

IncidentId = Annotated[str, StringConstraints(pattern=r"^inc-[0-9a-f]{16}$")]


class IncidentStatus(StrEnum):
    """Lifecycle position of an incident. ``detected`` means the rule fired."""

    DETECTED = "detected"


class RootCauseAssessment(StrEnum):
    """Whether an investigation has assessed a root cause. Always not assessed today."""

    NOT_ASSESSED = "not_assessed"


class CorrelationKind(StrEnum):
    """Kind of statement a correlation rationale makes."""

    TEMPORAL_PROXIMITY = "temporal_proximity"
    DEPENDENCY_LINK = "dependency_link"
    RELATED_METRIC_BEHAVIOR = "related_metric_behavior"
    SINGLE_ALERT = "single_alert"
    NOT_CAUSAL = "not_causal"


class InclusionReason(StrEnum):
    """Why an observation is in an incident's evidence.

    These are three different things, and only the first is a qualifying detection.

    - ``contributed_to_alert``: a qualifying detection. The sample was taken at an
      alert tick and is part of the evidence the rule fired on.
    - ``anomalous_in_window``: an anomalous measurement after the first qualifying
      detection that did not qualify an alert.
    - ``pre_incident_context``: an anomalous sample before the first qualifying
      detection. It is context only. It can be noise, and it is not evidence that
      the incident was already under way.
    """

    CONTRIBUTED_TO_ALERT = "contributed_to_alert"
    ANOMALOUS_IN_WINDOW = "anomalous_in_window"
    PRE_INCIDENT_CONTEXT = "pre_incident_context"


class TimelineKind(StrEnum):
    """Whether a timeline entry is a telemetry observation or a detection decision."""

    OBSERVATION = "observation"
    DETECTION_DECISION = "detection_decision"
    TELEMETRY_GAP = "telemetry_gap"


class TimelineMilestone(StrEnum):
    """What a timeline entry marks within the incident."""

    EARLIEST_SUPPORTING_OBSERVATION = "earliest_supporting_observation"
    RELATED_METRIC_CHANGE = "related_metric_change"
    FIRST_QUALIFYING_DETECTION = "first_qualifying_detection"
    LATEST_OBSERVED_EVIDENCE = "latest_observed_evidence"
    TELEMETRY_GAP = "telemetry_gap"


class CorrelationReason(BaseModel):
    """One statement on why alerts were grouped, or what grouping does not show."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CorrelationKind
    detail: str


class BaselineComparison(BaseModel):
    """The detector's comparison of one observation with its training baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detector: str
    direction: Literal["two_sided", "upper_tail"]
    threshold: float
    score: float
    training_run_ids: tuple[RunId, ...]


class EvidenceRecord(BaseModel):
    """One stored observation that supports an incident, with its provenance.

    ``source_event_id`` is the event in the event store the value was read from.
    Nothing here is derived from ground-truth labels.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_event_id: EventId
    run_id: RunId
    service: ServiceName
    metric_name: MetricName
    unit: Unit
    severity: Severity
    observed_at: UtcTimestamp
    observed_value: MetricValue
    inclusion_reason: InclusionReason
    baseline_comparison: BaselineComparison | None


class TelemetryGap(BaseModel):
    """Two consecutive observations of a run with no observation between them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_before: UtcTimestamp
    observed_after: UtcTimestamp

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        """Require the gap to run forward in time."""
        if self.observed_after <= self.observed_before:
            raise ValueError("observed_after must be later than observed_before")
        return self


class TimelineEntry(BaseModel):
    """One dated point in an incident timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: UtcTimestamp
    kind: TimelineKind
    milestone: TimelineMilestone
    service: ServiceName | None
    metric_name: MetricName | None
    source_event_id: EventId | None
    description: str


class IncidentSummary(BaseModel):
    """Compact view of an incident for list responses, without evidence or timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: IncidentId
    status: IncidentStatus
    root_cause_assessment: RootCauseAssessment
    source_run_id: RunId
    first_detection_at: UtcTimestamp
    latest_detection_at: UtcTimestamp
    alert_count: Annotated[int, Field(ge=1)]
    affected_services: tuple[ServiceName, ...]
    affected_metrics: tuple[MetricName, ...]
    evidence_count: Annotated[int, Field(ge=1)]

    @classmethod
    def from_incident(cls, incident: "Incident") -> Self:
        """Summarize an incident without its evidence or timeline."""
        return cls(
            incident_id=incident.incident_id,
            status=incident.status,
            root_cause_assessment=incident.root_cause_assessment,
            source_run_id=incident.source_run_id,
            first_detection_at=incident.first_detection_at,
            latest_detection_at=incident.latest_detection_at,
            alert_count=incident.alert_count,
            affected_services=incident.affected_services,
            affected_metrics=incident.affected_metrics,
            evidence_count=len(incident.evidence),
        )


class Incident(BaseModel):
    """A group of related alerts from one simulation run, with evidence and timeline.

    The four timestamps must stay in order: the earliest supporting observation
    is no later than the first detection, and the latest observed evidence is
    no earlier than the latest detection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: IncidentId
    status: IncidentStatus
    root_cause_assessment: RootCauseAssessment
    source_run_id: RunId
    baseline_run_ids: tuple[RunId, ...]
    detector: str
    rule: str
    correlation_window_seconds: Annotated[int, Field(ge=1)]
    first_observation_at: UtcTimestamp
    first_detection_at: UtcTimestamp
    latest_detection_at: UtcTimestamp
    latest_observation_at: UtcTimestamp
    alert_count: Annotated[int, Field(ge=1)]
    affected_services: tuple[ServiceName, ...]
    affected_metrics: tuple[MetricName, ...]
    correlation_rationale: tuple[CorrelationReason, ...]
    evidence: tuple[EvidenceRecord, ...] = Field(min_length=1)
    timeline: tuple[TimelineEntry, ...]
    telemetry_gaps: tuple[TelemetryGap, ...]

    @model_validator(mode="after")
    def _timestamps_ordered(self) -> Self:
        """Keep observation and detection timestamps in their causal order."""
        if not (
            self.first_observation_at
            <= self.first_detection_at
            <= self.latest_detection_at
            <= self.latest_observation_at
        ):
            raise ValueError("incident timestamps are out of order")
        return self
