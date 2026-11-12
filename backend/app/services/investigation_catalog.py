"""Fixed rules and static catalogs for the investigation engine. Holds no labels.

Each hypothesis rule declares what the data would have to show:

- ``primary``: the signal that must have contributed to alerts at several ticks
  in the span (see ``MIN_PRIMARY_ALERT_TICKS``).
- ``corroborating``: signals the hypothesis expects. Anomalous adds support,
  and measured normal is recorded as contradicting evidence.
- ``competing``: signals that must be measured normal, or the rule is not
  generated. They are the other explanations the rule has to rule out.
- ``ordering``: an expected temporal order. A reversed onset is contradicting
  evidence. Temporal precedence is necessary for a causal reading, not
  sufficient for one.

The rules were written from the simulator's documented fault effects and the
declared topology. They were not tuned on any evaluation seed. A rule states
what the data shows, not what caused it. Each statement says so.
"""

from dataclasses import dataclass

from app.domain.investigation_records import (
    CheckKind,
    HypothesisKind,
    HypothesisRole,
    SeriesRef,
)
from app.domain.telemetry import MetricName
from app.services.simulation import API_GATEWAY, ORDERS_DB, ORDERS_SERVICE

DB_QUERY_LATENCY = SeriesRef(
    service=ORDERS_DB, metric_name=MetricName.DB_QUERY_LATENCY_MS
)
DB_CONNECTIONS = SeriesRef(
    service=ORDERS_DB, metric_name=MetricName.ACTIVE_DB_CONNECTIONS
)
REQUEST_LATENCY = SeriesRef(
    service=ORDERS_SERVICE, metric_name=MetricName.REQUEST_LATENCY_MS
)
ORDERS_CPU = SeriesRef(
    service=ORDERS_SERVICE, metric_name=MetricName.CPU_UTILIZATION_PERCENT
)
ORDERS_ERRORS = SeriesRef(
    service=ORDERS_SERVICE, metric_name=MetricName.HTTP_ERROR_COUNT
)
GATEWAY_ERRORS = SeriesRef(service=API_GATEWAY, metric_name=MetricName.HTTP_ERROR_COUNT)
CONNECTION_FAILURES = SeriesRef(
    service=ORDERS_SERVICE, metric_name=MetricName.DB_CONNECTION_FAILURES
)


MIN_PRIMARY_ALERT_TICKS = 2
"""Alert ticks the primary signal must contribute to before a rule applies.

A single alert tick is one sample that happened to cross the threshold, which
is what a noisy count produces. Requiring two ticks asks for a pattern across
time. The value was chosen from one probe of the simulator's fault scenarios on
seeds 301, 302, and 311, which are not evaluation seeds. It is a choice, and it
can be changed in one place.
"""


@dataclass(frozen=True)
class HypothesisRule:
    """One candidate explanation and the observations it needs."""

    kind: HypothesisKind
    primary: SeriesRef
    role: HypothesisRole
    statement: str
    uncertainty: str
    corroborating: tuple[SeriesRef, ...] = ()
    competing: tuple[SeriesRef, ...] = ()
    ordering: tuple[SeriesRef, SeriesRef] | None = None

    @property
    def series(self) -> tuple[SeriesRef, ...]:
        """Every series the rule reads, in a fixed order."""
        return (self.primary, *self.corroborating, *self.competing)


RULES: tuple[HypothesisRule, ...] = (
    HypothesisRule(
        kind=HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION,
        primary=DB_QUERY_LATENCY,
        role=HypothesisRole.POTENTIAL_INITIATING_FAULT,
        corroborating=(REQUEST_LATENCY,),
        ordering=(DB_QUERY_LATENCY, REQUEST_LATENCY),
        statement=(
            "Database query latency on orders-db changed together with request "
            "latency on orders-service. The pattern is consistent with slower "
            "queries, but it does not show that the queries caused the change."
        ),
        uncertainty=(
            "Query-level traces are not stored, so the statements and plans "
            "involved cannot be seen. Another dependency could slow the same "
            "requests."
        ),
    ),
    HypothesisRule(
        kind=HypothesisKind.CPU_RESOURCE_SATURATION,
        primary=ORDERS_CPU,
        role=HypothesisRole.POTENTIAL_INITIATING_FAULT,
        corroborating=(REQUEST_LATENCY,),
        ordering=(ORDERS_CPU, REQUEST_LATENCY),
        statement=(
            "CPU utilization on orders-service rose together with its request "
            "latency. The pattern is consistent with CPU saturation, but it does "
            "not show that CPU caused the latency."
        ),
        uncertainty=(
            "Host-level process and thread metrics are not stored. Work outside "
            "the request path could also raise CPU."
        ),
    ),
    HypothesisRule(
        kind=HypothesisKind.APPLICATION_PROCESSING_BOTTLENECK,
        primary=REQUEST_LATENCY,
        role=HypothesisRole.POTENTIAL_INITIATING_FAULT,
        competing=(ORDERS_CPU, DB_QUERY_LATENCY),
        statement=(
            "Request latency on orders-service rose while CPU and database query "
            "latency stayed normal. The pattern is consistent with a bottleneck "
            "in application processing, but normal values on these two signals "
            "do not rule out other causes."
        ),
        uncertainty=(
            "Lock contention, serialization, and downstream calls outside the "
            "measured set would look the same from these signals."
        ),
    ),
    HypothesisRule(
        kind=HypothesisKind.API_ERROR_PROPAGATION,
        primary=ORDERS_ERRORS,
        role=HypothesisRole.DOWNSTREAM_SYMPTOM,
        corroborating=(GATEWAY_ERRORS,),
        statement=(
            "HTTP error counts rose on orders-service and at api-gateway. The "
            "pattern is consistent with errors passing up the call chain, but it "
            "does not show which service first produced them."
        ),
        uncertainty=(
            "Error messages are not stored, so the error type and its origin "
            "cannot be read."
        ),
    ),
    HypothesisRule(
        kind=HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,
        primary=CONNECTION_FAILURES,
        role=HypothesisRole.POTENTIAL_INITIATING_FAULT,
        statement=(
            "Database connection failures rose on orders-service. The pattern is "
            "consistent with a connectivity problem to orders-db, but it does not "
            "show where the failure starts."
        ),
        uncertainty=(
            "The error text, the connection pool limits, and the network path are "
            "not stored."
        ),
    ),
)

WATCHED_SERIES: tuple[SeriesRef, ...] = tuple(
    sorted(
        {series for rule in RULES for series in rule.series},
        key=lambda series: (series.service, series.metric_name.value),
    )
)
ORDERINGS: tuple[tuple[SeriesRef, SeriesRef], ...] = tuple(
    rule.ordering for rule in RULES if rule.ordering is not None
)


@dataclass(frozen=True)
class SourceGap:
    """Evidence the telemetry model does not collect, and the check to read it."""

    description: str
    affects: tuple[HypothesisKind, ...]
    check: CheckKind


SOURCE_GAPS: tuple[SourceGap, ...] = (
    SourceGap(
        description=(
            "No query-level traces or slow-query records are in the telemetry "
            "model, so the statement or plan that changed cannot be identified."
        ),
        affects=(HypothesisKind.DATABASE_QUERY_PERFORMANCE_DEGRADATION,),
        check=CheckKind.REVIEW_QUERY_TRACES,
    ),
    SourceGap(
        description=(
            "No application logs or error messages are stored, so the cause of an "
            "error count cannot be read."
        ),
        affects=(
            HypothesisKind.API_ERROR_PROPAGATION,
            HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,
        ),
        check=CheckKind.REVIEW_APPLICATION_LOGS,
    ),
    SourceGap(
        description=(
            "No deployment or configuration change records are stored, so it "
            "cannot be shown whether a change came before onset."
        ),
        affects=tuple(HypothesisKind),
        check=CheckKind.REVIEW_CHANGE_RECORDS,
    ),
    SourceGap(
        description=(
            "Connection pool limits and wait queues are not in the telemetry model."
        ),
        affects=(HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,),
        check=CheckKind.REVIEW_CONNECTION_POOL_LIMITS,
    ),
    SourceGap(
        description=(
            "No host-level process or thread metrics are stored for orders-service."
        ),
        affects=(
            HypothesisKind.CPU_RESOURCE_SATURATION,
            HypothesisKind.APPLICATION_PROCESSING_BOTTLENECK,
        ),
        check=CheckKind.REVIEW_HOST_METRICS,
    ),
)

DIRECTION_NOTE = (
    "The detector flags only upward deviations, so a fall in active_db_connections "
    "is not evaluated and cannot appear as evidence."
)

CHECK_DESCRIPTIONS: dict[CheckKind, str] = {
    CheckKind.REVIEW_QUERY_TRACES: (
        "Read query-level traces or slow-query records for orders-db over the "
        "analysis span, from the source system."
    ),
    CheckKind.REVIEW_APPLICATION_LOGS: (
        "Read error logs for api-gateway and orders-service over the analysis "
        "span, from the source system."
    ),
    CheckKind.REVIEW_CHANGE_RECORDS: (
        "Read deployment and configuration change records for the period before "
        "the first detection, to see whether a change preceded onset."
    ),
    CheckKind.REVIEW_CONNECTION_POOL_LIMITS: (
        "Read connection pool limits and wait metrics for orders-service and "
        "orders-db, from the source system."
    ),
    CheckKind.REVIEW_HOST_METRICS: (
        "Read host-level CPU, process, and thread metrics for orders-service over "
        "the analysis span."
    ),
    CheckKind.RESCORE_DOWNWARD_DEVIATIONS: (
        "Score active_db_connections with a two-sided detector to see whether the "
        "count fell during the span."
    ),
    CheckKind.REVIEW_UNSCORED_MEASUREMENTS: (
        "Find out why these measurements were unscored before relying on the "
        "absence of anomalies."
    ),
}
