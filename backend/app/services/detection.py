"""Anomaly detector interface: fit on training events, predict on evaluation events.

A detector learns what normal looks like from a set of events and then scores
new events against that model. Training and inference are separate calls, and
a detector can only be scored after it has been fitted.

Detectors see only the value, timestamp, and series identity of an event. They
never read its ground-truth label, so a label cannot become a predictor feature.
Labels are used by the caller to choose normal training data before ``fit`` and
by the evaluation module after ``predict``.

Every prediction is point-wise: it depends on the fitted model and the single
event being scored, never on other events or on later observations.
"""

import math
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import ClassVar

from app.domain.telemetry import TelemetryEvent
from app.services.analytics import SeriesKey, series_key


class Decision(StrEnum):
    """Outcome of scoring one event.

    ``UNSCORED`` means the detector had no basis for a judgement. It is never
    reported as normal, and evaluation counts it separately.
    """

    ANOMALOUS = "anomalous"
    NORMAL = "normal"
    UNSCORED = "unscored"


class Reason(StrEnum):
    """Why an event is unscored, or why a zero-spread series was flagged."""

    UNSEEN_SERIES = "unseen_series"
    INSUFFICIENT_TRAINING_SAMPLES = "insufficient_training_samples"
    INVALID_VALUE = "invalid_value"
    ZERO_SPREAD_DEVIATION = "zero_spread_deviation"


class Direction(Enum):
    """Which deviations count as anomalous.

    ``TWO_SIDED`` flags values above or below normal. ``UPPER_TAIL`` flags only
    values above normal, which suits latency and error counts, where a drop is
    not an incident.
    """

    TWO_SIDED = "two_sided"
    UPPER_TAIL = "upper_tail"


@dataclass(frozen=True)
class DetectorConfig:
    """Parameters shared by every detector. Subclasses set their own threshold.

    The threshold is compared with the score using ``>=``, the same convention
    as the severity thresholds in the telemetry catalog.
    """

    threshold: float
    direction: Direction = Direction.TWO_SIDED
    min_training_samples: int = 10

    def __post_init__(self) -> None:
        """Reject thresholds and sample requirements that cannot give a verdict."""
        if not math.isfinite(self.threshold) or self.threshold <= 0.0:
            raise ValueError("threshold must be a finite number greater than zero")
        if self.min_training_samples < 2:
            raise ValueError("min_training_samples must be at least 2")
        if not isinstance(self.direction, Direction):
            raise ValueError("direction must be a Direction")


@dataclass(frozen=True)
class Prediction:
    """The verdict for one evaluation event.

    ``score`` is the statistic compared with the threshold: the absolute
    deviation for two-sided detection, the signed deviation for upper-tail.
    It is ``None`` when the event is unscored or flagged as a zero-spread
    deviation, where no finite score exists.
    """

    event_id: str
    timestamp: datetime
    key: SeriesKey
    decision: Decision
    score: float | None
    reason: Reason | None


@dataclass(frozen=True)
class SeriesModel:
    """Location and spread of one series as learned from training data."""

    center: float
    spread: float
    sample_count: int


class AnomalyDetector(ABC):
    """Base class for detectors that model each series independently.

    Subclasses supply the location and spread estimators and how a deviation
    is scaled. The base class owns grouping, sample checks, zero-spread
    handling, direction, and the threshold, so every detector treats
    unsupported input the same way.
    """

    name: ClassVar[str]

    def __init__(self, config: DetectorConfig) -> None:
        """Store the configuration. The detector is unfitted until ``fit``."""
        self.config = config
        self._models: dict[SeriesKey, SeriesModel] | None = None
        self._too_few_samples: frozenset[SeriesKey] = frozenset()

    @property
    def is_fitted(self) -> bool:
        """True once ``fit`` has been called."""
        return self._models is not None

    def fit(self, training_events: Iterable[TelemetryEvent]) -> None:
        """Learn one model per series from the given events.

        Refitting replaces the previous model. Values must be finite; a single
        invalid value rejects the whole fit, because a silently skipped value
        would change the model without any trace.
        """
        values_by_key: dict[SeriesKey, list[float]] = defaultdict(list)
        for event in training_events:
            value = event.value
            if not math.isfinite(value):
                raise ValueError("training values must be finite")
            values_by_key[series_key(event)].append(value)

        models: dict[SeriesKey, SeriesModel] = {}
        too_few: set[SeriesKey] = set()
        for key, values in sorted(values_by_key.items()):
            if len(values) < self.config.min_training_samples:
                too_few.add(key)
                continue
            center, spread = self._location_and_spread(sorted(values))
            models[key] = SeriesModel(
                center=center,
                spread=spread,
                sample_count=len(values),
            )
        self._models = models
        self._too_few_samples = frozenset(too_few)

    def predict(self, evaluation_events: Iterable[TelemetryEvent]) -> list[Prediction]:
        """Score each event against the fitted model, in input order.

        Raises RuntimeError if the detector has not been fitted, so an
        unfitted detector can never produce verdicts.
        """
        models = self._fitted_models()
        return [self._score_event(event, models) for event in evaluation_events]

    def _fitted_models(self) -> dict[SeriesKey, SeriesModel]:
        """Return the fitted models, or raise if ``fit`` has not run."""
        if self._models is None:
            raise RuntimeError("detector must be fitted before it can predict")
        return self._models

    @abstractmethod
    def _location_and_spread(
        self,
        sorted_values: Sequence[float],
    ) -> tuple[float, float]:
        """Return the center and spread of a sorted, non-empty training sample.

        The spread may be zero. Callers handle that case; this method must not
        return NaN or a negative spread.
        """

    @abstractmethod
    def _standardize(self, deviation: float, spread: float) -> float:
        """Scale a signed deviation from the center by the spread.

        Called only when the spread is positive.
        """

    def _score_event(
        self,
        event: TelemetryEvent,
        models: dict[SeriesKey, SeriesModel],
    ) -> Prediction:
        """Return the verdict for one event, applying every unsupported-input rule."""
        key = series_key(event)

        def unscored(reason: Reason) -> Prediction:
            return Prediction(
                event_id=event.event_id,
                timestamp=event.timestamp,
                key=key,
                decision=Decision.UNSCORED,
                score=None,
                reason=reason,
            )

        if not math.isfinite(event.value):
            return unscored(Reason.INVALID_VALUE)
        if key in self._too_few_samples:
            return unscored(Reason.INSUFFICIENT_TRAINING_SAMPLES)
        model = models.get(key)
        if model is None:
            return unscored(Reason.UNSEEN_SERIES)

        deviation = event.value - model.center
        if model.spread == 0.0:
            return self._zero_spread_prediction(event, key, deviation)

        score = self._score(self._standardize(deviation, model.spread))
        flagged = score >= self.config.threshold
        return Prediction(
            event_id=event.event_id,
            timestamp=event.timestamp,
            key=key,
            decision=Decision.ANOMALOUS if flagged else Decision.NORMAL,
            score=score,
            reason=None,
        )

    def _zero_spread_prediction(
        self,
        event: TelemetryEvent,
        key: SeriesKey,
        deviation: float,
    ) -> Prediction:
        """Handle a series whose training values had no spread.

        A standardized score is undefined with no spread. An exact match to the
        constant training value is normal, with score 0. Any other deviation in
        a counted direction is anomalous with no score: a constant healthy
        series has no observed variation to compare against, so any change is
        outside the behaviour seen in training.
        """
        if deviation == 0.0:
            return Prediction(
                event_id=event.event_id,
                timestamp=event.timestamp,
                key=key,
                decision=Decision.NORMAL,
                score=0.0,
                reason=None,
            )
        counted = (
            self.config.direction is Direction.TWO_SIDED or deviation > 0.0
        )
        return Prediction(
            event_id=event.event_id,
            timestamp=event.timestamp,
            key=key,
            decision=Decision.ANOMALOUS if counted else Decision.NORMAL,
            score=None,
            reason=Reason.ZERO_SPREAD_DEVIATION if counted else None,
        )

    def _score(self, standardized: float) -> float:
        """Return the value compared with the threshold for the configured tail."""
        if self.config.direction is Direction.UPPER_TAIL:
            return standardized
        return abs(standardized)
