"""Telemetry analytics: per-series summaries and rolling statistics.

A series is identified by service, metric, and unit. Statistics are computed
per series and never across series, so measurements in different units, or from
different services, cannot be combined by accident. Rolling statistics go one
step further and require a single run, so a window never spans two timelines.

Analytics reads the ``value`` and ``timestamp`` of an event and never its
ground-truth label. The same functions serve baselines and incident
statistics, so a caller that wants them separate filters the events first.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.telemetry import METRIC_SPECS, MetricName, TelemetryEvent, Unit
from app.services.statistics import DescriptiveStats, describe, rolling_statistics


@dataclass(frozen=True, order=True)
class SeriesKey:
    """Identifies one comparable series: a metric from one service, in one unit."""

    service: str
    metric_name: MetricName
    unit: Unit


@dataclass(frozen=True)
class SeriesSummary:
    """Descriptive statistics for one series."""

    key: SeriesKey
    stats: DescriptiveStats


@dataclass(frozen=True)
class RollingSeriesPoint:
    """One point of a rolling series, with the trailing window's statistics."""

    timestamp: datetime
    value: float
    window_count: int
    mean: float | None
    stdev: float | None


def series_key(event: TelemetryEvent) -> SeriesKey:
    """Return the series an event belongs to."""
    return SeriesKey(
        service=event.service,
        metric_name=event.metric_name,
        unit=event.unit,
    )


def summarize(events: Iterable[TelemetryEvent]) -> list[SeriesSummary]:
    """Summarize each series separately, sorted by key for stable output."""
    values_by_series: dict[SeriesKey, list[float]] = defaultdict(list)
    for event in events:
        values_by_series[series_key(event)].append(event.value)
    return [
        SeriesSummary(key=key, stats=describe(values))
        for key, values in sorted(values_by_series.items())
    ]


def rolling_series(
    events: Sequence[TelemetryEvent],
    window: int,
) -> list[RollingSeriesPoint]:
    """Return rolling statistics over one series from one run, in time order.

    Events are sorted by (timestamp, event_id). The event ID breaks ties
    deterministically, so the same events always produce the same points.
    Raises ValueError if the events span more than one series or run, because
    a window over mixed telemetry would be meaningless.
    """
    if not events:
        return []
    keys = {series_key(event) for event in events}
    runs = {event.run_id for event in events}
    if len(keys) > 1:
        raise ValueError("rolling statistics need exactly one service, metric and unit")
    if len(runs) > 1:
        raise ValueError("rolling statistics need events from exactly one run")
    ordered = sorted(events, key=lambda event: (event.timestamp, event.event_id))
    rolling = rolling_statistics([event.value for event in ordered], window)
    return [
        RollingSeriesPoint(
            timestamp=event.timestamp,
            value=event.value,
            window_count=point.window_count,
            mean=point.mean,
            stdev=point.stdev,
        )
        for event, point in zip(ordered, rolling, strict=True)
    ]


def metric_unit(metric_name: MetricName) -> Unit:
    """Return the unit a metric is always reported in, from the catalog."""
    return METRIC_SPECS[metric_name].unit
