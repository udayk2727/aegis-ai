"""Telemetry domain models: the contract every event must satisfy.

These types perform no I/O and know nothing about HTTP. They are the single
source of truth for what a valid event looks like, so the simulator, the
event store, and the API all reject the same malformed data.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

EventId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64),
]
RunId = Annotated[str, StringConstraints(pattern=r"^sim-[0-9a-f]{16}$")]
ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
MetricValue = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


def _to_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC so stored times compare consistently.

    Converting near the start of the calendar can fall outside the datetime
    range. That is reported as a ValueError so it becomes a validation error
    instead of an unhandled OverflowError.
    """
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("timestamp is outside the supported calendar range") from error


# Naive datetimes are rejected by AwareDatetime, so every stored time is unambiguous.
UtcTimestamp = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class ScenarioId(StrEnum):
    """Scenario that generated a run. Only the first is fault-free."""

    NORMAL_OPERATION = "normal_operation"
    DATABASE_LATENCY_SPIKE = "database_latency_spike"
    CPU_SATURATION = "cpu_saturation"
    API_ERRORS = "api_errors"
    CONNECTION_FAILURE = "connection_failure"


class EventType(StrEnum):
    """Kind of event. Error counts are kept apart so alerting can filter them."""

    METRIC_SAMPLE = "metric_sample"
    ERROR_COUNT = "error_count"


class MetricName(StrEnum):
    """Metrics the simulator can emit. Each has exactly one unit."""

    REQUEST_LATENCY_MS = "request_latency_ms"
    DB_QUERY_LATENCY_MS = "db_query_latency_ms"
    CPU_UTILIZATION_PERCENT = "cpu_utilization_percent"
    ACTIVE_DB_CONNECTIONS = "active_db_connections"
    HTTP_ERROR_COUNT = "http_error_count"
    DB_CONNECTION_FAILURES = "db_connection_failures"


class Unit(StrEnum):
    """Unit of a metric value."""

    MILLISECONDS = "milliseconds"
    PERCENT = "percent"
    COUNT = "count"


class Severity(StrEnum):
    """Severity derived from a metric value and its thresholds."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class MetricSpec:
    """Static definition of a metric: unit, event type, and severity thresholds."""

    unit: Unit
    event_type: EventType
    warning_threshold: float
    critical_threshold: float


METRIC_SPECS: dict[MetricName, MetricSpec] = {
    MetricName.REQUEST_LATENCY_MS: MetricSpec(
        Unit.MILLISECONDS, EventType.METRIC_SAMPLE, 400.0, 800.0
    ),
    MetricName.DB_QUERY_LATENCY_MS: MetricSpec(
        Unit.MILLISECONDS, EventType.METRIC_SAMPLE, 30.0, 60.0
    ),
    MetricName.CPU_UTILIZATION_PERCENT: MetricSpec(
        Unit.PERCENT, EventType.METRIC_SAMPLE, 80.0, 95.0
    ),
    MetricName.ACTIVE_DB_CONNECTIONS: MetricSpec(
        Unit.COUNT, EventType.METRIC_SAMPLE, 40.0, 50.0
    ),
    MetricName.HTTP_ERROR_COUNT: MetricSpec(
        Unit.COUNT, EventType.ERROR_COUNT, 1.0, 5.0
    ),
    MetricName.DB_CONNECTION_FAILURES: MetricSpec(
        Unit.COUNT, EventType.ERROR_COUNT, 1.0, 5.0
    ),
}


def classify_severity(metric: MetricName, value: float) -> Severity:
    """Return the severity of a value; a value equal to a threshold is raised to it."""
    spec = METRIC_SPECS[metric]
    if value >= spec.critical_threshold:
        return Severity.CRITICAL
    if value >= spec.warning_threshold:
        return Severity.WARNING
    return Severity.INFO


class GroundTruthLabel(BaseModel):
    """Ground truth attached to each event, used to score detection later."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: ScenarioId
    is_anomalous: bool

    @model_validator(mode="after")
    def _normal_operation_is_never_anomalous(self) -> Self:
        """Reject labels that mark normal operation as anomalous."""
        if self.scenario_id is ScenarioId.NORMAL_OPERATION and self.is_anomalous:
            raise ValueError("normal_operation cannot be labelled anomalous")
        return self


class TelemetryEvent(BaseModel):
    """One metric observation from one service at one point in time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: EventId
    run_id: RunId
    timestamp: UtcTimestamp
    service: ServiceName
    event_type: EventType
    metric_name: MetricName
    value: MetricValue
    unit: Unit
    severity: Severity
    label: GroundTruthLabel

    @model_validator(mode="after")
    def _matches_metric_definition(self) -> Self:
        """Require unit, event type, and severity to agree with the metric catalog."""
        spec = METRIC_SPECS[self.metric_name]
        if self.unit != spec.unit or self.event_type != spec.event_type:
            raise ValueError("unit and event_type must match the metric definition")
        if self.severity != classify_severity(self.metric_name, self.value):
            raise ValueError("severity does not match the value and thresholds")
        return self
