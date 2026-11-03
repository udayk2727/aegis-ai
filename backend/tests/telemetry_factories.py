"""Factories for hand-built telemetry events used by the analytics tests.

Hand-built events let a test state its expected numbers directly, instead of
deriving them from the simulator's random draws.
"""

from datetime import UTC, datetime, timedelta

from app.domain.telemetry import (
    METRIC_SPECS,
    GroundTruthLabel,
    MetricName,
    ScenarioId,
    TelemetryEvent,
    classify_severity,
)

TEST_RUN_ID = "sim-0123456789abcdef"
TEST_START = datetime(2026, 1, 1, tzinfo=UTC)


def make_event(
    *,
    service: str,
    metric: MetricName,
    value: float,
    minute: int,
    sequence: int,
    run_id: str = TEST_RUN_ID,
    is_anomalous: bool = False,
) -> TelemetryEvent:
    """Build a valid event at ``minute`` after the test start time."""
    spec = METRIC_SPECS[metric]
    return TelemetryEvent(
        event_id=f"{run_id.removeprefix('sim-')}-{sequence:06d}",
        run_id=run_id,
        timestamp=TEST_START + timedelta(minutes=minute),
        service=service,
        event_type=spec.event_type,
        metric_name=metric,
        value=value,
        unit=spec.unit,
        severity=classify_severity(metric, value),
        label=GroundTruthLabel(
            scenario_id=ScenarioId.NORMAL_OPERATION
            if not is_anomalous
            else ScenarioId.DATABASE_LATENCY_SPIKE,
            is_anomalous=is_anomalous,
        ),
    )
