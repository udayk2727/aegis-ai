"""Deterministic grouping of run-level alerts into candidate incidents.

Inputs are the alert ticks of one run, produced by the Day 4 ``IncidentRule``
unchanged. Each alert is the set of series that were anomalous at its tick.

Grouping rule. Two alerts in the same run are linked when both hold:

1. They are at most ``window_seconds`` apart, measured from alert time to alert
   time, inclusive. A gap of exactly the window links.
2. Their services are equal or one calls the other in the topology.

Incidents are the connected components of that link graph. Links are transitive,
so a chain of related alerts can span longer than one window. Alerts from
different runs are never linked, because each run is grouped on its own.

Correlation shows co-occurrence in time and in the declared topology. It does
not show cause. The rationale attached to each group says so explicitly.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.incident_records import CorrelationKind, CorrelationReason
from app.services.analytics import SeriesKey
from app.services.detection import Decision, Prediction
from app.services.topology import ServiceTopology

# Ten one-minute ticks. A 5-minute window split single faults in two when the
# rule went quiet for a few ticks. This value was picked by inspecting simulator
# runs with seeds 201-205, not by a pre-registered validation, so treat it as a
# tuned default. See docs/architecture.md, section 13.
DEFAULT_CORRELATION_WINDOW_SECONDS = 600


@dataclass(frozen=True)
class CorrelationConfig:
    """How far apart in time two related alerts may be and still be grouped."""

    window_seconds: int = DEFAULT_CORRELATION_WINDOW_SECONDS

    def __post_init__(self) -> None:
        """Reject a window that cannot link any two alerts."""
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be at least 1")


@dataclass(frozen=True)
class Alert:
    """One alert tick: the anomalous series at that tick, in sorted order."""

    timestamp: datetime
    keys: tuple[SeriesKey, ...]

    @property
    def services(self) -> frozenset[str]:
        """Services with at least one anomalous series at this tick."""
        return frozenset(key.service for key in self.keys)


def anomalies_by_tick(
    predictions: Iterable[Prediction],
) -> dict[datetime, dict[SeriesKey, Prediction]]:
    """Index anomalous predictions by tick, then by series.

    A repeated prediction for the same tick and series is collapsed to its first
    occurrence, so a duplicate can never count as a second alert or a second
    piece of evidence.
    """
    index: dict[datetime, dict[SeriesKey, Prediction]] = {}
    for prediction in predictions:
        if prediction.decision is not Decision.ANOMALOUS:
            continue
        by_key = index.setdefault(prediction.timestamp, {})
        by_key.setdefault(prediction.key, prediction)
    return index


def build_alerts(
    alert_ticks: Iterable[datetime],
    anomalies: Mapping[datetime, Mapping[SeriesKey, Prediction]],
) -> list[Alert]:
    """Turn the rule's alert ticks into alerts, in time order, without duplicates."""
    return [
        Alert(timestamp=tick, keys=tuple(sorted(anomalies[tick])))
        for tick in sorted(set(alert_ticks))
    ]


def group_alerts(
    alerts: Sequence[Alert],
    topology: ServiceTopology,
    window_seconds: int,
) -> list[list[Alert]]:
    """Partition alerts into incidents, each list in time order.

    Groups are ordered by their first alert, so the result does not depend on
    the order the alerts were supplied in.
    """
    ordered = sorted(alerts, key=lambda alert: alert.timestamp)
    window = timedelta(seconds=window_seconds)
    parent = list(range(len(ordered)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(ordered)):
        for right in range(left + 1, len(ordered)):
            gap = ordered[right].timestamp - ordered[left].timestamp
            if gap > window:
                break
            if topology.any_related(ordered[left].services, ordered[right].services):
                parent[root(right)] = root(left)

    groups: dict[int, list[Alert]] = {}
    for index, alert in enumerate(ordered):
        groups.setdefault(root(index), []).append(alert)
    return sorted(groups.values(), key=lambda group: group[0].timestamp)


def correlation_rationale(
    group: Sequence[Alert],
    topology: ServiceTopology,
    window_seconds: int,
) -> tuple[CorrelationReason, ...]:
    """Describe why a group was formed, in a fixed order, with no causal claim."""
    services = sorted({service for alert in group for service in alert.services})
    keys = sorted({key for alert in group for key in alert.keys})
    metrics = sorted({key.metric_name for key in keys})

    reasons: list[CorrelationReason] = []
    if len(group) == 1:
        reasons.append(
            CorrelationReason(
                kind=CorrelationKind.SINGLE_ALERT,
                detail=(
                    "one alert tick; no other alert in this run was related to it "
                    f"within {window_seconds} seconds"
                ),
            )
        )
    else:
        reasons.append(
            CorrelationReason(
                kind=CorrelationKind.TEMPORAL_PROXIMITY,
                detail=(
                    f"{len(group)} alert ticks linked by a chain of related alerts "
                    f"at most {window_seconds} seconds apart"
                ),
            )
        )

    for caller in services:
        for callee in services:
            if (caller, callee) in topology.calls:
                reasons.append(
                    CorrelationReason(
                        kind=CorrelationKind.DEPENDENCY_LINK,
                        detail=f"{caller} calls {callee} in the declared topology",
                    )
                )

    reasons.append(
        CorrelationReason(
            kind=CorrelationKind.RELATED_METRIC_BEHAVIOR,
            detail=(
                f"{len(keys)} series anomalous together across {len(services)} "
                f"service(s); metrics: {', '.join(metrics)}"
            ),
        )
    )
    reasons.append(
        CorrelationReason(
            kind=CorrelationKind.NOT_CAUSAL,
            detail=(
                "co-occurrence in time and in the declared topology does not "
                "establish which service caused the others"
            ),
        )
    )
    return tuple(reasons)
