"""Read-only analytics endpoints over the in-memory event store.

GET /api/v1/analytics/summary     descriptive statistics per series
GET /api/v1/analytics/baselines   normal-operation baseline statistics
GET /api/v1/analytics/timeseries  one run's series with rolling statistics

All three are read-only and bounded. Summaries and baselines are bounded by the
fixed topology (at most one series per service and metric). Timeseries pages
are capped, and each response is built from the store without exposing any
ground-truth label. Statistics are computed in the services layer, not here.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.domain.telemetry import (
    MetricName,
    RunId,
    ScenarioId,
    ServiceName,
    Unit,
    UtcTimestamp,
)
from app.routers.dependencies import get_event_store
from app.services.analytics import metric_unit, rolling_series, summarize
from app.services.baseline import SELECTION_DESCRIPTION, build_baseline
from app.services.event_store import EventFilter, EventStore
from app.services.statistics import DescriptiveStats

DEFAULT_ROLLING_WINDOW = 5
MIN_ROLLING_WINDOW = 2
MAX_ROLLING_WINDOW = 60
DEFAULT_POINT_LIMIT = 100
MAX_POINT_LIMIT = 500
MAX_BASELINE_RUNS = 20

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


class Methodology(BaseModel):
    """Statistical conventions that every analytics response is computed with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quantile: Literal["linear"]
    stdev: Literal["sample"]


METHODOLOGY = Methodology(quantile="linear", stdev="sample")


class MetricStatistics(BaseModel):
    """Statistics for one series. Fields are None when the sample is too small."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceName
    metric_name: MetricName
    unit: Unit
    sample_count: Annotated[int, Field(ge=0)]
    missing_count: Annotated[int, Field(ge=0)]
    mean: float | None
    median: float | None
    minimum: float | None
    maximum: float | None
    stdev: float | None
    p50: float | None
    p95: float | None
    p99: float | None


def _to_metric_statistics(
    service: str,
    metric_name: MetricName,
    unit: Unit,
    stats: DescriptiveStats,
) -> MetricStatistics:
    """Map a service-layer summary onto the response contract."""
    return MetricStatistics(
        service=service,
        metric_name=metric_name,
        unit=unit,
        sample_count=stats.sample_count,
        missing_count=stats.missing_count,
        mean=stats.mean,
        median=stats.median,
        minimum=stats.minimum,
        maximum=stats.maximum,
        stdev=stats.stdev,
        p50=stats.p50,
        p95=stats.p95,
        p99=stats.p99,
    )


class SummaryResponse(BaseModel):
    """Response body for GET /api/v1/analytics/summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_count: Annotated[int, Field(ge=0)]
    methodology: Methodology
    series: list[MetricStatistics]


class BaselineResponse(BaseModel):
    """Response body for GET /api/v1/analytics/baselines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: Annotated[int, Field(ge=0)]
    source_run_ids: list[RunId]
    selection: str
    methodology: Methodology
    series: list[MetricStatistics]


class TimeseriesPoint(BaseModel):
    """One event in a rolling series. Mean and stdev are None until the window fills."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: UtcTimestamp
    value: float
    window_count: Annotated[int, Field(ge=1)]
    mean: float | None
    stdev: float | None


class TimeseriesPage(BaseModel):
    """Response body for GET /api/v1/analytics/timeseries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    service: ServiceName
    metric_name: MetricName
    unit: Unit
    window: Annotated[int, Field(ge=MIN_ROLLING_WINDOW, le=MAX_ROLLING_WINDOW)]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=MAX_POINT_LIMIT)]
    offset: Annotated[int, Field(ge=0)]
    methodology: Methodology
    points: list[TimeseriesPoint]


@router.get("/summary", response_model=SummaryResponse)
def read_summary(
    store: Annotated[EventStore, Depends(get_event_store)],
    run_id: RunId | None = None,
    scenario_id: ScenarioId | None = None,
    service: ServiceName | None = None,
    metric_name: MetricName | None = None,
    is_anomalous: bool | None = None,
) -> SummaryResponse:
    """Summarize stored events per service, metric, and unit.

    Filter with ``is_anomalous`` to compare normal and incident distributions.
    An empty match returns zero events and an empty series list.
    """
    event_filter = EventFilter(
        run_id=run_id,
        scenario_id=scenario_id,
        service=service,
        metric_name=metric_name,
        is_anomalous=is_anomalous,
    )
    events = store.matching_events(event_filter)
    series = [
        _to_metric_statistics(
            summary.key.service,
            summary.key.metric_name,
            summary.key.unit,
            summary.stats,
        )
        for summary in summarize(events)
    ]
    return SummaryResponse(
        event_count=len(events),
        methodology=METHODOLOGY,
        series=series,
    )


@router.get("/baselines", response_model=BaselineResponse)
def read_baselines(
    store: Annotated[EventStore, Depends(get_event_store)],
    run_id: Annotated[list[RunId] | None, Query(max_length=MAX_BASELINE_RUNS)] = None,
    before: UtcTimestamp | None = None,
) -> BaselineResponse:
    """Return normal-operation baseline statistics, optionally from chosen runs.

    Only normal-labelled events are used. Without ``run_id`` every stored run
    contributes. ``before`` limits the baseline to events earlier than that
    time. An empty selection returns a sample count of zero and no series.
    """
    selected = set(run_id) if run_id else None
    events = [
        event
        for event in store.matching_events(EventFilter())
        if (selected is None or event.run_id in selected)
        and (before is None or event.timestamp < before)
    ]
    baseline = build_baseline(events)
    return BaselineResponse(
        sample_count=baseline.sample_count,
        source_run_ids=list(baseline.source_run_ids),
        selection=SELECTION_DESCRIPTION,
        methodology=METHODOLOGY,
        series=[
            _to_metric_statistics(
                item.key.service,
                item.key.metric_name,
                item.key.unit,
                item.stats,
            )
            for item in baseline.series
        ],
    )


@router.get("/timeseries", response_model=TimeseriesPage)
def read_timeseries(
    store: Annotated[EventStore, Depends(get_event_store)],
    run_id: RunId,
    service: ServiceName,
    metric_name: MetricName,
    window: Annotated[
        int,
        Query(ge=MIN_ROLLING_WINDOW, le=MAX_ROLLING_WINDOW),
    ] = DEFAULT_ROLLING_WINDOW,
    limit: Annotated[int, Query(ge=1, le=MAX_POINT_LIMIT)] = DEFAULT_POINT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TimeseriesPage:
    """Return one page of a single series from one run, with rolling statistics.

    The rolling window is computed over the whole series before paging, so a
    later page carries the same statistics it would have on the first page.
    An unknown run, service, or metric pair returns an empty page.
    """
    events = store.matching_events(
        EventFilter(run_id=run_id, service=service, metric_name=metric_name),
    )
    points = rolling_series(events, window)
    page = points[offset : offset + limit]
    return TimeseriesPage(
        run_id=run_id,
        service=service,
        metric_name=metric_name,
        unit=metric_unit(metric_name),
        window=window,
        total=len(points),
        limit=limit,
        offset=offset,
        methodology=METHODOLOGY,
        points=[
            TimeseriesPoint(
                timestamp=point.timestamp,
                value=point.value,
                window_count=point.window_count,
                mean=point.mean,
                stdev=point.stdev,
            )
            for point in page
        ],
    )
