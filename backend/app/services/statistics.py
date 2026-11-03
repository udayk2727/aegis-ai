"""Descriptive statistics over numeric samples, with no framework or I/O imports.

Conventions. Every caller gets the same numbers, so the choices are fixed here:

- Quantiles interpolate linearly between order statistics (R-7, the default in
  NumPy and pandas). For sorted values x[0..n-1] and q in [0, 1], the position is
  h = (n - 1) * q, and the result lies between x[floor(h)] and x[ceil(h)].
- Standard deviation is the sample standard deviation with divisor n - 1. It is
  undefined for fewer than two samples and is reported as None rather than 0.
- A missing value is None. Missing values are excluded from every statistic and
  counted separately. NaN and infinity are invalid and rejected, because a
  single one would silently poison the mean and the deviations.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_MEDIAN_Q = 0.5
_P95_Q = 0.95
_P99_Q = 0.99


@dataclass(frozen=True)
class DescriptiveStats:
    """Summary of one sample set. Statistics are None when the set is too small."""

    sample_count: int
    missing_count: int
    mean: float | None
    median: float | None
    minimum: float | None
    maximum: float | None
    stdev: float | None
    p50: float | None
    p95: float | None
    p99: float | None


@dataclass(frozen=True)
class RollingPoint:
    """Rolling statistics at one position. Incomplete windows carry None."""

    index: int
    window_count: int
    mean: float | None
    stdev: float | None


def _checked_value(value: float | None) -> float | None:
    """Return a finite float, None for a missing value, or raise on bad input."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("samples must be real numbers or None")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("samples must be finite; NaN and infinity are rejected")
    return number


def _checked_values(values: Iterable[float | None]) -> tuple[list[float], int]:
    """Split samples into the finite values and the number of missing ones."""
    present: list[float] = []
    missing = 0
    for raw in values:
        checked = _checked_value(raw)
        if checked is None:
            missing += 1
        else:
            present.append(checked)
    return present, missing


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Return the q-quantile of values that are already sorted ascending.

    Uses the linear-interpolation convention documented at module level. The
    caller is responsible for sorting; this keeps repeated quantiles over one
    sample cheap.
    """
    if not sorted_values:
        raise ValueError("a quantile of an empty sample is undefined")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be between 0 and 1 inclusive")
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (
        fraction
    )


def sample_mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of a non-empty sample.

    The sum is compensated with math.fsum. The result is clamped into
    [min, max], which removes rounding drift: a constant sample then has
    mean exactly equal to its value, so its deviations are exactly zero.
    """
    if not values:
        raise ValueError("the mean of an empty sample is undefined")
    mean = math.fsum(values) / len(values)
    return min(max(mean, min(values)), max(values))


def sample_stdev(values: Sequence[float]) -> float | None:
    """Return the sample standard deviation (divisor n - 1), or None if n < 2."""
    if len(values) < 2:
        return None
    mean = sample_mean(values)
    squared_deviations = math.fsum((value - mean) ** 2 for value in values)
    return math.sqrt(squared_deviations / (len(values) - 1))


def describe(values: Iterable[float | None]) -> DescriptiveStats:
    """Compute every summary statistic for one sample set.

    Empty and single-value samples are valid inputs. Each statistic that cannot
    be defined for the sample size is None, never a fabricated number.
    """
    present, missing = _checked_values(values)
    if not present:
        return DescriptiveStats(
            sample_count=0,
            missing_count=missing,
            mean=None,
            median=None,
            minimum=None,
            maximum=None,
            stdev=None,
            p50=None,
            p95=None,
            p99=None,
        )
    ordered = sorted(present)
    mean = sample_mean(ordered)
    p50 = quantile(ordered, _MEDIAN_Q)
    return DescriptiveStats(
        sample_count=len(ordered),
        missing_count=missing,
        mean=mean,
        median=p50,
        minimum=ordered[0],
        maximum=ordered[-1],
        stdev=sample_stdev(ordered),
        p50=p50,
        p95=quantile(ordered, _P95_Q),
        p99=quantile(ordered, _P99_Q),
    )


def rolling_statistics(values: Sequence[float], window: int) -> list[RollingPoint]:
    """Return the trailing-window mean and standard deviation at every position.

    Position i uses values[i - window + 1 .. i]. Until the window holds `window`
    values, mean and stdev are None and window_count reports how many values
    exist so far, so an incomplete window is never mistaken for a full one.
    Values must be finite; rolling statistics do not skip missing values,
    because removing one would silently shift the window.
    """
    if window < 2:
        raise ValueError("window must be at least 2 for a standard deviation")
    checked = [_checked_number(value) for value in values]
    points: list[RollingPoint] = []
    for index in range(len(checked)):
        span = checked[max(0, index - window + 1) : index + 1]
        if len(span) < window:
            points.append(RollingPoint(index, len(span), None, None))
        else:
            points.append(
                RollingPoint(index, window, sample_mean(span), sample_stdev(span))
            )
    return points


def _checked_number(value: float) -> float:
    """Return a finite float, rejecting missing values for rolling statistics."""
    checked = _checked_value(value)
    if checked is None:
        raise ValueError("rolling statistics do not accept missing values")
    return checked
