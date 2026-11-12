"""Tests for the z-score and robust detectors and the shared detector interface.

Expected values are worked out by hand in comments, so the tests check the
arithmetic rather than re-running the implementation.
"""

import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from app.domain.telemetry import (
    GroundTruthLabel,
    MetricName,
    ScenarioId,
    TelemetryEvent,
)
from app.services.detection import (
    AnomalyDetector,
    Decision,
    Direction,
    Prediction,
    Reason,
)
from app.services.detectors import (
    RobustConfig,
    RobustDetector,
    ZScoreConfig,
    ZScoreDetector,
)
from tests.telemetry_factories import make_event

SERVICE = "orders-service"
# Training sample 8, 9, 10, 11, 12: mean 10, sample stdev sqrt(2.5), median 10,
# absolute deviations 2, 1, 0, 1, 2 with median 1, so MAD 1.
HEALTHY = (8.0, 9.0, 10.0, 11.0, 12.0)
EVALUATION_RUN = "sim-fedcba9876543210"

DetectorFactory = Callable[..., AnomalyDetector]


def make_zscore(**changes: object) -> ZScoreDetector:
    """Build a z-score detector that accepts the five-sample training sets here."""
    base = ZScoreConfig(min_training_samples=5)
    return ZScoreDetector(replace(base, **changes))


def make_robust(**changes: object) -> RobustDetector:
    """Build a robust detector that accepts the five-sample training sets here."""
    base = RobustConfig(min_training_samples=5)
    return RobustDetector(replace(base, **changes))


DETECTORS: tuple[DetectorFactory, ...] = (make_zscore, make_robust)


def latency(
    value: float,
    *,
    minute: int,
    sequence: int,
    service: str = SERVICE,
    run_id: str | None = None,
) -> TelemetryEvent:
    """Build one request-latency event. Keyword arguments keep call sites clear."""
    options = {} if run_id is None else {"run_id": run_id}
    return make_event(
        service=service,
        metric=MetricName.REQUEST_LATENCY_MS,
        value=value,
        minute=minute,
        sequence=sequence,
        **options,
    )


def training(values: tuple[float, ...]) -> list[TelemetryEvent]:
    """Build healthy training events for one latency series, one minute apart."""
    return [
        latency(value, minute=index, sequence=index)
        for index, value in enumerate(values)
    ]


def probes(*values: float) -> list[TelemetryEvent]:
    """Build evaluation events in a separate run, all from the same series."""
    return [
        latency(
            value,
            minute=100 + index,
            sequence=900 + index,
            run_id=EVALUATION_RUN,
        )
        for index, value in enumerate(values)
    ]


def score_one(detector: AnomalyDetector, value: float) -> Prediction:
    """Fit on the healthy sample and return the verdict for one probe value."""
    detector.fit(training(HEALTHY))
    (prediction,) = detector.predict(probes(value))
    return prediction


# --- Configuration ----------------------------------------------------------


@pytest.mark.parametrize("threshold", [0.0, -1.0, math.nan, math.inf])
def test_threshold_must_be_finite_and_positive(threshold: float) -> None:
    with pytest.raises(ValueError, match="threshold"):
        ZScoreConfig(threshold=threshold)


def test_min_training_samples_must_allow_a_spread() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        RobustConfig(min_training_samples=1)


def test_default_thresholds_follow_the_documented_conventions() -> None:
    assert ZScoreConfig().threshold == 3.0
    assert RobustConfig().threshold == 3.5


# --- Z-score numerical examples ---------------------------------------------


def test_zscore_flags_value_above_three_sigma() -> None:
    # z = (16 - 10) / sqrt(2.5) = 3.7947, which is at least 3.0.
    prediction = score_one(make_zscore(), 16.0)

    assert prediction.decision is Decision.ANOMALOUS
    assert prediction.score == pytest.approx(6.0 / math.sqrt(2.5))


def test_zscore_keeps_value_inside_threshold_normal() -> None:
    # z = (12.5 - 10) / sqrt(2.5) = 1.5811, below 3.0.
    prediction = score_one(make_zscore(), 12.5)

    assert prediction.decision is Decision.NORMAL
    assert prediction.score == pytest.approx(2.5 / math.sqrt(2.5))
    assert prediction.reason is None


def test_zscore_threshold_is_inclusive() -> None:
    # Training 0, 2, 4 has mean 2 and sample variance (4 + 0 + 4) / 2 = 4, so
    # stdev is exactly 2. A probe at 6 gives z = 2.0 exactly, which meets the
    # threshold of 2.0.
    detector = ZScoreDetector(ZScoreConfig(threshold=2.0, min_training_samples=3))
    detector.fit(training((0.0, 2.0, 4.0)))

    (prediction,) = detector.predict(probes(6.0))

    assert prediction.score == 2.0
    assert prediction.decision is Decision.ANOMALOUS


# --- Robust numerical examples ----------------------------------------------


def test_robust_flags_value_above_threshold() -> None:
    # M = 0.6745 * (16 - 10) / 1 = 4.047, which is at least 3.5.
    prediction = score_one(make_robust(), 16.0)

    assert prediction.decision is Decision.ANOMALOUS
    assert prediction.score == pytest.approx(0.6745 * 6.0)


def test_robust_keeps_value_inside_threshold_normal() -> None:
    # M = 0.6745 * (13 - 10) / 1 = 2.0235, below 3.5.
    prediction = score_one(make_robust(), 13.0)

    assert prediction.decision is Decision.NORMAL
    assert prediction.score == pytest.approx(0.6745 * 3.0)


def test_robust_scale_matches_the_documented_convention() -> None:
    # The score is 0.6745 * deviation / MAD, not a z-score on the raw spread.
    prediction = score_one(make_robust(threshold=0.1), 12.0)

    assert prediction.score == pytest.approx(0.6745 * 2.0 / 1.0)


# --- Behaviour shared by both detectors ------------------------------------


@pytest.mark.parametrize("make", DETECTORS)
def test_two_sided_flags_low_values(make: DetectorFactory) -> None:
    detector = make()
    detector.fit(training(HEALTHY))

    (prediction,) = detector.predict(probes(4.0))

    assert prediction.decision is Decision.ANOMALOUS


@pytest.mark.parametrize("make", DETECTORS)
def test_upper_tail_ignores_low_values(make: DetectorFactory) -> None:
    detector = make(direction=Direction.UPPER_TAIL)
    detector.fit(training(HEALTHY))

    low, high = detector.predict(probes(4.0, 16.0))

    assert low.decision is Decision.NORMAL
    assert low.score is not None and low.score < 0.0
    assert high.decision is Decision.ANOMALOUS


def test_predict_before_fit_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="fitted"):
        make_zscore().predict(probes(10.0))


def test_fit_rejects_non_finite_training_values() -> None:
    bad = training(HEALTHY)
    bad[2] = bad[2].model_copy(update={"value": math.nan})

    with pytest.raises(ValueError, match="finite"):
        make_zscore().fit(bad)


def test_refit_replaces_the_previous_model() -> None:
    detector = make_zscore()
    detector.fit(training(HEALTHY))
    detector.fit(training((100.0, 101.0, 102.0, 103.0, 104.0)))

    (prediction,) = detector.predict(probes(103.0))

    # Under the new mean of 102, 103 is one unit above: z = 1 / sqrt(2.5).
    # Under the old model it would be far outside, so this proves the refit.
    assert prediction.decision is Decision.NORMAL
    assert prediction.score == pytest.approx(1.0 / math.sqrt(2.5))


def test_detector_reports_fitted_state_and_names() -> None:
    detector = make_zscore()

    assert not detector.is_fitted
    detector.fit(training(HEALTHY))
    assert detector.is_fitted
    assert ZScoreDetector.name == "zscore"
    assert RobustDetector.name == "robust"


# --- Zero variance and MAD = 0 ---------------------------------------------


@pytest.mark.parametrize("make", DETECTORS)
def test_constant_training_accepts_the_exact_value(make: DetectorFactory) -> None:
    detector = make()
    detector.fit(training((7.0,) * 5))

    (prediction,) = detector.predict(probes(7.0))

    assert prediction.decision is Decision.NORMAL
    assert prediction.score == 0.0
    assert prediction.reason is None


@pytest.mark.parametrize("make", DETECTORS)
def test_constant_training_flags_any_deviation_without_a_score(
    make: DetectorFactory,
) -> None:
    detector = make()
    detector.fit(training((7.0,) * 5))

    (prediction,) = detector.predict(probes(7.5))

    assert prediction.decision is Decision.ANOMALOUS
    assert prediction.score is None
    assert prediction.reason is Reason.ZERO_SPREAD_DEVIATION


def test_mad_zero_from_ties_is_handled_as_zero_spread() -> None:
    # Five 5s and one 6: the median is 5, and all absolute deviations are 0 except
    # one, so the median of the deviations, the MAD, is 0. Nothing is divided by
    # zero: the deviation is flagged under the zero-spread rule.
    detector = make_robust()
    detector.fit(training((5.0, 5.0, 5.0, 5.0, 5.0, 6.0)))

    normal, anomalous = detector.predict(probes(5.0, 7.0))

    assert normal.decision is Decision.NORMAL
    assert anomalous.decision is Decision.ANOMALOUS
    assert anomalous.reason is Reason.ZERO_SPREAD_DEVIATION


def test_zero_spread_upper_tail_ignores_drop_below_constant() -> None:
    detector = make_zscore(direction=Direction.UPPER_TAIL)
    detector.fit(training((7.0,) * 5))

    (prediction,) = detector.predict(probes(6.0))

    assert prediction.decision is Decision.NORMAL


# --- Tie handling -----------------------------------------------------------


def test_duplicate_training_values_count_at_full_multiplicity() -> None:
    # Sorted [1, 1, 2, 2, 3, 3]: median (2 + 2) / 2 = 2. Absolute deviations are
    # [1, 1, 0, 0, 1, 1], sorted [0, 0, 1, 1, 1, 1], so MAD = (1 + 1) / 2 = 1.
    # Probe 4: M = 0.6745 * (4 - 2) / 1 = 1.349, which is normal at 3.5.
    detector = RobustDetector(RobustConfig(min_training_samples=2))
    detector.fit(training((1.0, 1.0, 2.0, 2.0, 3.0, 3.0)))

    (prediction,) = detector.predict(probes(4.0))

    assert prediction.score == pytest.approx(0.6745 * 2.0)
    assert prediction.decision is Decision.NORMAL


# --- Unsupported inputs are never classified as normal ---------------------


@pytest.mark.parametrize("make", DETECTORS)
def test_too_few_training_samples_is_unscored(make: DetectorFactory) -> None:
    detector = make(min_training_samples=6)
    detector.fit(training(HEALTHY))

    (prediction,) = detector.predict(probes(10.0))

    assert prediction.decision is Decision.UNSCORED
    assert prediction.reason is Reason.INSUFFICIENT_TRAINING_SAMPLES
    assert prediction.score is None


@pytest.mark.parametrize("make", DETECTORS)
def test_unseen_metric_is_unscored_not_normal(make: DetectorFactory) -> None:
    detector = make()
    detector.fit(training(HEALTHY))
    unseen = make_event(
        service=SERVICE,
        metric=MetricName.DB_QUERY_LATENCY_MS,
        value=10.0,
        minute=100,
        sequence=900,
    )

    (prediction,) = detector.predict([unseen])

    assert prediction.decision is Decision.UNSCORED
    assert prediction.reason is Reason.UNSEEN_SERIES


def test_same_metric_on_unseen_service_is_unscored() -> None:
    detector = make_zscore()
    detector.fit(training(HEALTHY))

    (prediction,) = detector.predict(
        [latency(10.0, minute=100, sequence=900, service="api-gateway")]
    )

    assert prediction.decision is Decision.UNSCORED
    assert prediction.reason is Reason.UNSEEN_SERIES


def test_non_finite_evaluation_value_is_unscored() -> None:
    detector = make_zscore()
    detector.fit(training(HEALTHY))
    invalid = probes(10.0)[0].model_copy(update={"value": math.nan})

    (prediction,) = detector.predict([invalid])

    assert prediction.decision is Decision.UNSCORED
    assert prediction.reason is Reason.INVALID_VALUE


# --- Series isolation and point-wise prediction -----------------------------


def test_series_are_modelled_independently_by_service() -> None:
    # Both services report the same metric. Each has its own model, so a value
    # normal for api-gateway is anomalous for orders-service, which runs 100 ms
    # higher in training.
    gateway = [
        latency(value, minute=index, sequence=index, service="api-gateway")
        for index, value in enumerate(HEALTHY)
    ]
    orders = [
        latency(value + 100.0, minute=index, sequence=100 + index)
        for index, value in enumerate(HEALTHY)
    ]
    detector = make_zscore()
    detector.fit([*gateway, *orders])

    gateway_probe, orders_probe = detector.predict(
        [
            latency(10.0, minute=100, sequence=900, service="api-gateway"),
            latency(10.0, minute=100, sequence=901),
        ]
    )

    assert gateway_probe.decision is Decision.NORMAL
    assert orders_probe.decision is Decision.ANOMALOUS


def test_predictions_do_not_depend_on_later_observations() -> None:
    # Scoring the first three events alone must match scoring them inside a longer
    # list, so later observations cannot change an earlier verdict.
    detector = make_robust()
    detector.fit(training(HEALTHY))
    evaluation = probes(10.0, 16.0, 11.0, 40.0)

    assert detector.predict(evaluation[:3]) == detector.predict(evaluation)[:3]


def test_identical_inputs_produce_identical_predictions() -> None:
    detector = make_zscore()
    detector.fit(training(HEALTHY))
    evaluation = probes(9.0, 15.0, 4.0)

    assert detector.predict(evaluation) == detector.predict(evaluation)


# --- Labels are not predictor features --------------------------------------


def relabel_as_fault(events: list[TelemetryEvent]) -> list[TelemetryEvent]:
    """Return copies that all claim to be anomalous fault-window events."""
    label = GroundTruthLabel(scenario_id=ScenarioId.CPU_SATURATION, is_anomalous=True)
    return [event.model_copy(update={"label": label}) for event in events]


@pytest.mark.parametrize("make", DETECTORS)
def test_labels_do_not_change_fitting_or_predictions(make: DetectorFactory) -> None:
    original = make()
    original.fit(training(HEALTHY))
    relabelled = make()
    relabelled.fit(relabel_as_fault(training(HEALTHY)))
    evaluation = probes(10.0, 16.0, 4.0)

    assert original.predict(evaluation) == relabelled.predict(
        relabel_as_fault(evaluation)
    )
