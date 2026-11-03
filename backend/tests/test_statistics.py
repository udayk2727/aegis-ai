"""Tests for descriptive and rolling statistics against hand-calculated values."""

import math

import pytest

from app.services.statistics import describe, quantile, rolling_statistics

# Hand-calculated fixture: n = 8, sum = 40, mean = 5.
# Squared deviations from 5: 9, 1, 1, 1, 0, 0, 4, 16 -> total 32.
# Sample variance = 32 / 7, so the sample stdev is sqrt(32 / 7). The population
# stdev would be sqrt(32 / 8) = 2, which is the value this suite must not return.
# Sorted sample x = [2, 4, 4, 4, 5, 5, 7, 9] with h = (n - 1) * q:
#   P50: h = 3.5 -> 4 + 0.5 * (5 - 4) = 4.5
#   P95: h = 6.65 -> 7 + 0.65 * (9 - 7) = 8.3
#   P99: h = 6.93 -> 7 + 0.93 * (9 - 7) = 8.86
EIGHT_VALUES = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]


def test_hand_calculated_statistics_for_eight_values() -> None:
    stats = describe(EIGHT_VALUES)

    assert stats.sample_count == 8
    assert stats.missing_count == 0
    assert stats.mean == pytest.approx(5.0)
    assert stats.median == pytest.approx(4.5)
    assert stats.minimum == 2.0
    assert stats.maximum == 9.0
    assert stats.stdev == pytest.approx(math.sqrt(32 / 7))
    assert stats.p50 == pytest.approx(4.5)
    assert stats.p95 == pytest.approx(8.3)
    assert stats.p99 == pytest.approx(8.86)


def test_input_order_does_not_change_statistics() -> None:
    assert describe(list(reversed(EIGHT_VALUES))) == describe(EIGHT_VALUES)


def test_quantile_endpoints_and_interpolation() -> None:
    ordered = [1.0, 2.0, 3.0, 4.0]

    assert quantile(ordered, 0.0) == 1.0
    assert quantile(ordered, 1.0) == 4.0
    assert quantile(ordered, 0.5) == pytest.approx(2.5)
    # h = 3 * (1/3) = 1 lands exactly on an order statistic.
    assert quantile(ordered, 1 / 3) == pytest.approx(2.0)


def test_quantile_rejects_empty_sample_and_out_of_range_q() -> None:
    with pytest.raises(ValueError):
        quantile([], 0.5)
    with pytest.raises(ValueError):
        quantile([1.0], 1.5)
    with pytest.raises(ValueError):
        quantile([1.0], -0.1)


def test_empty_sample_has_no_statistics() -> None:
    stats = describe([])

    assert stats.sample_count == 0
    assert stats.missing_count == 0
    assert stats.mean is None
    assert stats.median is None
    assert stats.minimum is None
    assert stats.maximum is None
    assert stats.stdev is None
    assert stats.p50 is None
    assert stats.p95 is None
    assert stats.p99 is None


def test_single_value_has_value_statistics_but_no_stdev() -> None:
    stats = describe([5.0])

    assert stats.sample_count == 1
    assert stats.mean == 5.0
    assert stats.median == 5.0
    assert stats.minimum == 5.0
    assert stats.maximum == 5.0
    assert stats.p50 == stats.p95 == stats.p99 == 5.0
    assert stats.stdev is None


def test_missing_values_are_excluded_and_counted() -> None:
    # [1, 3] after dropping None: mean 2, sample stdev sqrt(2).
    stats = describe([1.0, None, 3.0])

    assert stats.sample_count == 2
    assert stats.missing_count == 1
    assert stats.mean == pytest.approx(2.0)
    assert stats.stdev == pytest.approx(math.sqrt(2.0))


def test_all_missing_values_behave_like_an_empty_sample() -> None:
    stats = describe([None, None])

    assert stats.sample_count == 0
    assert stats.missing_count == 2
    assert stats.mean is None


@pytest.mark.parametrize(
    "bad_value",
    [math.nan, math.inf, -math.inf, True, "3.0"],
)
def test_invalid_numeric_inputs_are_rejected(bad_value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        describe([1.0, bad_value])  # type: ignore[list-item]


def test_zero_variance_sample_has_exact_zero_stdev_and_exact_mean() -> None:
    # Summing 0.1 three times and dividing rounds, so only the clamp in
    # sample_mean gives an exactly equal mean; without it the stdev is not 0.
    stats = describe([0.1, 0.1, 0.1])

    assert stats.mean == 0.1
    assert stats.stdev == 0.0
    assert stats.p95 == 0.1


def test_rolling_window_three_on_linear_sequence() -> None:
    points = rolling_statistics([1.0, 2.0, 3.0, 4.0, 5.0], window=3)

    assert [point.window_count for point in points] == [1, 2, 3, 3, 3]
    assert [point.mean for point in points] == [None, None, 2.0, 3.0, 4.0]
    # Each full window is consecutive integers, so its sample stdev is 1.
    assert [point.stdev for point in points] == [None, None, 1.0, 1.0, 1.0]


def test_rolling_zero_variance_window_reports_zero_stdev() -> None:
    points = rolling_statistics([7.0, 7.0, 7.0], window=3)

    assert points[-1].mean == 7.0
    assert points[-1].stdev == 0.0


def test_rolling_window_larger_than_series_never_reports_a_full_window() -> None:
    points = rolling_statistics([1.0, 2.0], window=5)

    assert all(point.mean is None and point.stdev is None for point in points)
    assert [point.window_count for point in points] == [1, 2]


def test_rolling_rejects_window_below_two() -> None:
    with pytest.raises(ValueError):
        rolling_statistics([1.0, 2.0], window=1)


def test_rolling_rejects_missing_values_rather_than_shifting_the_window() -> None:
    with pytest.raises(ValueError):
        rolling_statistics([1.0, None, 3.0], window=2)  # type: ignore[list-item]


def test_rolling_empty_series_returns_no_points() -> None:
    assert rolling_statistics([], window=3) == []
