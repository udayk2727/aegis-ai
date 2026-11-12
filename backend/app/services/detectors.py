"""Statistical anomaly detectors: a classic z-score and a robust MAD-based score.

Both detectors model each series from healthy training data and flag values
that sit far from the learned center. They differ in what they use:

- ``ZScoreDetector`` uses the mean and sample standard deviation. It is
  efficient when the training data is close to normal, but a few extreme
  training values inflate the spread and hide later anomalies.
- ``RobustDetector`` uses the median and the median absolute deviation (MAD).
  A minority of outliers in training barely moves either statistic.

Robust scaling convention. The robust score is
``M = 0.6745 * (x - median) / MAD``. For normally distributed data MAD is
about 0.6745 standard deviations, so M is comparable to a standard z-score.
The default threshold of 3.5 is the Iglewicz and Hoaglin recommendation for
M. The default threshold of 3.0 for ``ZScoreDetector`` is the usual
convention for z.

Ties. Duplicate training values are counted at full multiplicity. The median
of an even-sized sample is the mean of its two middle values. The MAD takes
the median of absolute deviations from the training median, so a sample where
more than half the values equal the median has MAD 0. That case is handled
by the shared zero-spread rule in ``AnomalyDetector``, not by a fudge factor.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from app.services.detection import AnomalyDetector, Direction, DetectorConfig
from app.services.statistics import quantile, sample_mean, sample_stdev

ROBUST_SCALE: float = 0.6745
MEDIAN_Q: float = 0.5


@dataclass(frozen=True)
class ZScoreConfig(DetectorConfig):
    """Configuration for the z-score detector. The default threshold is 3.0."""

    threshold: float = 3.0
    direction: Direction = Direction.TWO_SIDED
    min_training_samples: int = 10


@dataclass(frozen=True)
class RobustConfig(DetectorConfig):
    """Configuration for the robust detector. The default threshold is 3.5."""

    threshold: float = 3.5
    direction: Direction = Direction.TWO_SIDED
    min_training_samples: int = 10


class ZScoreDetector(AnomalyDetector):
    """Flags values whose standardized deviation from the training mean is large.

    The score is ``z = (x - mean) / stdev``, with the sample standard deviation
    (divisor n - 1) estimated from training values only.
    """

    name: ClassVar[str] = "zscore"

    def __init__(self, config: ZScoreConfig | None = None) -> None:
        """Create an unfitted detector. Defaults to ``ZScoreConfig()``."""
        super().__init__(config or ZScoreConfig())

    def _location_and_spread(
        self,
        sorted_values: Sequence[float],
    ) -> tuple[float, float]:
        """Return the training mean and sample standard deviation."""
        stdev = sample_stdev(sorted_values)
        if stdev is None:
            raise ValueError("a z-score needs at least two training samples")
        return sample_mean(sorted_values), stdev

    def _standardize(self, deviation: float, spread: float) -> float:
        """Return the z-score of a deviation from the mean."""
        return deviation / spread


class RobustDetector(AnomalyDetector):
    """Flags values whose robust z-score, from the median and MAD, is large.

    The score is ``M = 0.6745 * (x - median) / MAD``. The median and MAD come
    from training values only.
    """

    name: ClassVar[str] = "robust"

    def __init__(self, config: RobustConfig | None = None) -> None:
        """Create an unfitted detector. Defaults to ``RobustConfig()``."""
        super().__init__(config or RobustConfig())

    def _location_and_spread(
        self,
        sorted_values: Sequence[float],
    ) -> tuple[float, float]:
        """Return the training median and the median absolute deviation."""
        median = quantile(sorted_values, MEDIAN_Q)
        absolute_deviations = sorted(abs(value - median) for value in sorted_values)
        return median, quantile(absolute_deviations, MEDIAN_Q)

    def _standardize(self, deviation: float, spread: float) -> float:
        """Return the robust z-score M of a deviation from the median."""
        return ROBUST_SCALE * deviation / spread
