"""Deterministic simulation of enterprise telemetry with one injected fault.

The simulator models a three-tier chain: api-gateway calls orders-service,
which queries orders-db. Each tick, every service emits one event per metric
it reports. A scenario injects one fault into a contiguous window of ticks,
and every event in that window carries a ground-truth label.

Determinism: all randomness comes from random.Random(seed), and timestamps are
start_time + tick * interval. The clock is never read, so the same spec always
produces the same events. Specs that differ only in scenario share a seed, so
events before the fault window match exactly and runs can be compared there.
After the window the streams diverge, because the Poisson sampler consumes a
variable number of random draws.

Timestamps: the whole timeline, including the end of the fault window, must fit
inside the datetime range. A spec that would overflow is rejected at validation.

Labels and severity are independent. ``label.is_anomalous`` is ground truth: it
is true for every event inside the injected window, whatever its value. ``severity``
is a threshold classification of that single value, so an anomalous event can
be ``info`` when the fault pushes a metric below its warning threshold.

Baseline distributions, before any fault:
- request_latency_ms: lognormal, median 120 ms, sigma 0.15
- db_query_latency_ms: lognormal, median 8 ms, sigma 0.2
- cpu_utilization_percent: normal(35, 4), clipped to [0, 100]
- active_db_connections: normal(20, 2), clipped at 0, rounded to a whole count
- http_error_count: Poisson, rate 0.2 per tick
- db_connection_failures: Poisson, rate 0.05 per tick

Fault effects, applied only inside the fault window:
- database_latency_spike: db latency median x12; request latency x3;
  active connections normal(35, 2) as the pool fills while queries wait.
- cpu_saturation: orders-service CPU normal(96, 1.5); request latency x2.5.
- api_errors: HTTP error rate 6 per tick; request latency x1.4.
- connection_failure: DB connection failures rate 8 per tick on
  orders-service; active connections normal(5, 2); HTTP error rate 3 per tick;
  request latency x1.5.

Fault window: ticks [ticks // 3, 2 * ticks // 3). A run shorter than three
ticks gets a single anomalous tick so every fault scenario has ground truth.
"""

import hashlib
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.telemetry import (
    METRIC_SPECS,
    GroundTruthLabel,
    MetricName,
    RunId,
    ScenarioId,
    ServiceName,
    TelemetryEvent,
    UtcTimestamp,
    classify_severity,
)

MAX_TICKS = 288
MAX_TICK_INTERVAL_SECONDS = 3600
DEFAULT_TICK_INTERVAL_SECONDS = 60
DEFAULT_START_TIME = datetime(2026, 1, 1, tzinfo=UTC)
MAX_SEED = 2**32 - 1

API_GATEWAY: ServiceName = "api-gateway"
ORDERS_SERVICE: ServiceName = "orders-service"
ORDERS_DB: ServiceName = "orders-db"

_REQUEST_LATENCY_MEDIAN_MS = 120.0
_REQUEST_LATENCY_SIGMA = 0.15
_DB_LATENCY_MEDIAN_MS = 8.0
_DB_LATENCY_SIGMA = 0.2
_DB_SPIKE_LATENCY_MEDIAN_MS = _DB_LATENCY_MEDIAN_MS * 12.0
_BASELINE_CPU_MEAN = 35.0
_BASELINE_CPU_SD = 4.0
_SATURATED_CPU_MEAN = 96.0
_SATURATED_CPU_SD = 1.5
_BASELINE_CONNECTIONS_MEAN = 20.0
_BASELINE_CONNECTIONS_SD = 2.0
_BASELINE_ERROR_RATE = 0.2
_BASELINE_CONNECTION_FAILURE = 0.05

_REQUEST_LATENCY_FACTOR: dict[ScenarioId, float] = {
    ScenarioId.DATABASE_LATENCY_SPIKE: 3.0,
    ScenarioId.CPU_SATURATION: 2.5,
    ScenarioId.API_ERRORS: 1.4,
    ScenarioId.CONNECTION_FAILURE: 1.5,
}
_CONNECTIONS_MEAN: dict[ScenarioId, float] = {
    ScenarioId.DATABASE_LATENCY_SPIKE: 35.0,
    ScenarioId.CONNECTION_FAILURE: 5.0,
}
_HTTP_ERROR_RATE: dict[ScenarioId, float] = {
    ScenarioId.API_ERRORS: 6.0,
    ScenarioId.CONNECTION_FAILURE: 3.0,
}
_DB_CONNECTION_FAILURE_RATE: dict[ScenarioId, float] = {
    ScenarioId.CONNECTION_FAILURE: 8.0,
}


class SimulationSpec(BaseModel):
    """Validated parameters that fully determine a simulation run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: ScenarioId
    seed: Annotated[int, Field(ge=0, le=MAX_SEED)]
    ticks: Annotated[int, Field(ge=1, le=MAX_TICKS)]
    tick_interval_seconds: Annotated[
        int, Field(ge=1, le=MAX_TICK_INTERVAL_SECONDS)
    ] = DEFAULT_TICK_INTERVAL_SECONDS
    start_time: UtcTimestamp = DEFAULT_START_TIME

    @model_validator(mode="after")
    def _timeline_fits_datetime_range(self) -> Self:
        """Reject runs whose last timestamp falls outside the datetime range."""
        last_start = datetime.max.replace(tzinfo=UTC) - timedelta(
            seconds=self.ticks * self.tick_interval_seconds
        )
        if self.start_time > last_start:
            raise ValueError(
                "simulation timeline extends past the supported calendar range"
            )
        return self


class AnomalyWindow(BaseModel):
    """Ground-truth time range of the injected fault; end is exclusive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: UtcTimestamp
    end: UtcTimestamp


class SimulationRun(BaseModel):
    """A completed run: its spec, its fault window, and every generated event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    spec: SimulationSpec
    anomaly_window: AnomalyWindow | None
    events: tuple[TelemetryEvent, ...]

    @property
    def anomalous_event_count(self) -> int:
        """Number of events whose ground-truth label marks them anomalous."""
        return sum(1 for event in self.events if event.label.is_anomalous)


def run_id_for(spec: SimulationSpec) -> str:
    """Derive a stable run ID from the spec so identical requests share an ID."""
    digest = hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
    return f"sim-{digest[:16]}"


def _tick_time(spec: SimulationSpec, tick: int) -> datetime:
    """Return the timestamp of a tick without reading the system clock."""
    return spec.start_time + timedelta(seconds=tick * spec.tick_interval_seconds)


def _fault_ticks(ticks: int) -> range:
    """Return the tick indices where the fault is active."""
    start = ticks // 3
    end = max(start + 1, (2 * ticks) // 3)
    return range(start, end)


def _clip(value: float, low: float, high: float) -> float:
    """Clamp a sampled value to the physically meaningful range of a metric."""
    return min(high, max(low, value))


def _poisson(rng: random.Random, rate: float) -> float:
    """Sample a Poisson count with Knuth's multiplication method."""
    threshold = math.exp(-rate)
    count = 0
    product = rng.random()
    while product > threshold:
        count += 1
        product *= rng.random()
    return float(count)


def _draw_value(
    rng: random.Random,
    service: ServiceName,
    metric: MetricName,
    active: ScenarioId,
) -> float:
    """Sample one metric value under the active scenario (NORMAL_OPERATION if none)."""
    if metric is MetricName.REQUEST_LATENCY_MS:
        median = _REQUEST_LATENCY_MEDIAN_MS * _REQUEST_LATENCY_FACTOR.get(active, 1.0)
        return rng.lognormvariate(math.log(median), _REQUEST_LATENCY_SIGMA)
    if metric is MetricName.DB_QUERY_LATENCY_MS:
        median = (
            _DB_SPIKE_LATENCY_MEDIAN_MS
            if active is ScenarioId.DATABASE_LATENCY_SPIKE
            else _DB_LATENCY_MEDIAN_MS
        )
        return rng.lognormvariate(math.log(median), _DB_LATENCY_SIGMA)
    if metric is MetricName.CPU_UTILIZATION_PERCENT:
        if active is ScenarioId.CPU_SATURATION and service == ORDERS_SERVICE:
            mean, sd = _SATURATED_CPU_MEAN, _SATURATED_CPU_SD
        else:
            mean, sd = _BASELINE_CPU_MEAN, _BASELINE_CPU_SD
        return _clip(rng.gauss(mean, sd), 0.0, 100.0)
    if metric is MetricName.ACTIVE_DB_CONNECTIONS:
        mean = _CONNECTIONS_MEAN.get(active, _BASELINE_CONNECTIONS_MEAN)
        return max(0.0, round(rng.gauss(mean, _BASELINE_CONNECTIONS_SD)))
    if metric is MetricName.HTTP_ERROR_COUNT:
        return _poisson(rng, _HTTP_ERROR_RATE.get(active, _BASELINE_ERROR_RATE))
    if metric is MetricName.DB_CONNECTION_FAILURES:
        rate = _DB_CONNECTION_FAILURE_RATE.get(active, _BASELINE_CONNECTION_FAILURE)
        return _poisson(rng, rate)
    raise ValueError(f"no sampling rule for metric {metric}")


# Metrics reported by each service, in emission order. The order is part of
# the determinism contract: changing it changes which random draws each event
# receives.
_SERVICE_METRICS: tuple[tuple[ServiceName, tuple[MetricName, ...]], ...] = (
    (
        API_GATEWAY,
        (MetricName.REQUEST_LATENCY_MS, MetricName.HTTP_ERROR_COUNT),
    ),
    (
        ORDERS_SERVICE,
        (
            MetricName.REQUEST_LATENCY_MS,
            MetricName.HTTP_ERROR_COUNT,
            MetricName.CPU_UTILIZATION_PERCENT,
            MetricName.DB_CONNECTION_FAILURES,
        ),
    ),
    (
        ORDERS_DB,
        (
            MetricName.DB_QUERY_LATENCY_MS,
            MetricName.ACTIVE_DB_CONNECTIONS,
            MetricName.CPU_UTILIZATION_PERCENT,
        ),
    ),
)
EVENTS_PER_TICK = sum(len(metrics) for _, metrics in _SERVICE_METRICS)


def run_simulation(spec: SimulationSpec) -> SimulationRun:
    """Generate every event for a spec. Pure: one spec always yields one run."""
    run_id = run_id_for(spec)
    rng = random.Random(spec.seed)
    fault_ticks = (
        _fault_ticks(spec.ticks)
        if spec.scenario is not ScenarioId.NORMAL_OPERATION
        else range(0)
    )

    events: list[TelemetryEvent] = []
    for tick in range(spec.ticks):
        timestamp = _tick_time(spec, tick)
        is_anomalous = tick in fault_ticks
        active = spec.scenario if is_anomalous else ScenarioId.NORMAL_OPERATION
        label = GroundTruthLabel(
            scenario_id=spec.scenario, is_anomalous=is_anomalous
        )
        for service, metrics in _SERVICE_METRICS:
            for metric in metrics:
                value = round(_draw_value(rng, service, metric, active), 3)
                metric_spec = METRIC_SPECS[metric]
                events.append(
                    TelemetryEvent(
                        event_id=f"{run_id}-{len(events) + 1:06d}",
                        run_id=run_id,
                        timestamp=timestamp,
                        service=service,
                        event_type=metric_spec.event_type,
                        metric_name=metric,
                        value=value,
                        unit=metric_spec.unit,
                        severity=classify_severity(metric, value),
                        label=label,
                    )
                )

    anomaly_window = None
    if spec.scenario is not ScenarioId.NORMAL_OPERATION:
        anomaly_window = AnomalyWindow(
            start=_tick_time(spec, fault_ticks.start),
            end=_tick_time(spec, fault_ticks.stop),
        )
    return SimulationRun(
        run_id=run_id,
        spec=spec,
        anomaly_window=anomaly_window,
        events=tuple(events),
    )
