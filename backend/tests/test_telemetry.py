"""Tests for telemetry domain models and their validation rules."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.telemetry import (
    EventId,
    EventType,
    GroundTruthLabel,
    MetricName,
    MetricValue,
    ScenarioId,
    ServiceName,
    Severity,
    TelemetryEvent,
    Unit,
    UtcTimestamp,
    classify_severity,
)

RUN_ID = "sim-0123456789abcdef"


def _event_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid event payload, with any field replaced by ``overrides``."""
    payload: dict[str, Any] = {
        "event_id": f"{RUN_ID}-000001",
        "run_id": RUN_ID,
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "service": "api-gateway",
        "event_type": EventType.METRIC_SAMPLE,
        "metric_name": MetricName.REQUEST_LATENCY_MS,
        "value": 120.5,
        "unit": Unit.MILLISECONDS,
        "severity": Severity.INFO,
        "label": {"scenario_id": ScenarioId.NORMAL_OPERATION, "is_anomalous": False},
    }
    payload.update(overrides)
    return payload


def test_utc_timestamp_normalizes_offsets_to_utc() -> None:
    adapter = TypeAdapter(UtcTimestamp)
    local = datetime(2026, 1, 1, 2, 30, tzinfo=timezone(timedelta(hours=2)))

    result = adapter.validate_python(local)

    assert result == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    assert result.utcoffset() == timedelta(0)


def test_utc_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(UtcTimestamp).validate_python(datetime(2026, 1, 1))


@pytest.mark.parametrize("bad", ["", "Evt-1", "-leading", "trailing-", "a" * 65])
def test_event_id_rejects_malformed_values(bad: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(EventId).validate_python(bad)


@pytest.mark.parametrize("bad", ["a", "API-gateway", "api gateway", "1-service"])
def test_service_name_rejects_malformed_values(bad: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ServiceName).validate_python(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.1])
def test_metric_value_must_be_finite_and_non_negative(bad: float) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(MetricValue).validate_python(bad)


def test_metric_value_accepts_zero() -> None:
    assert TypeAdapter(MetricValue).validate_python(0) == 0.0


def test_valid_event_payload_is_accepted() -> None:
    event = TelemetryEvent.model_validate(_event_payload())

    assert event.timestamp.tzinfo is not None
    assert event.label.is_anomalous is False


def test_event_rejects_unit_that_does_not_match_metric() -> None:
    payload = _event_payload(unit=Unit.PERCENT)

    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_event_rejects_event_type_that_does_not_match_metric() -> None:
    payload = _event_payload(event_type=EventType.ERROR_COUNT)

    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_event_rejects_severity_inconsistent_with_value() -> None:
    payload = _event_payload(value=900.0, severity=Severity.INFO)

    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_event_rejects_unknown_fields() -> None:
    payload = _event_payload(host="db-01")

    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_event_rejects_unknown_metric_name() -> None:
    payload = _event_payload(metric_name="made_up_metric")

    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_normal_operation_cannot_be_labelled_anomalous() -> None:
    with pytest.raises(ValidationError):
        GroundTruthLabel(
            scenario_id=ScenarioId.NORMAL_OPERATION, is_anomalous=True
        )


def test_fault_scenario_may_be_labelled_anomalous() -> None:
    label = GroundTruthLabel(scenario_id=ScenarioId.API_ERRORS, is_anomalous=True)

    assert label.is_anomalous is True


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        (MetricName.REQUEST_LATENCY_MS, 399.9, Severity.INFO),
        (MetricName.REQUEST_LATENCY_MS, 400.0, Severity.WARNING),
        (MetricName.REQUEST_LATENCY_MS, 800.0, Severity.CRITICAL),
        (MetricName.HTTP_ERROR_COUNT, 0.0, Severity.INFO),
        (MetricName.HTTP_ERROR_COUNT, 5.0, Severity.CRITICAL),
    ],
)
def test_severity_thresholds_are_inclusive_at_the_lower_bound(
    metric: MetricName, value: float, expected: Severity
) -> None:
    assert classify_severity(metric, value) is expected
