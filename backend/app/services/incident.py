"""Incident decision layer: turns metric-level verdicts into run-level alerts.

A single metric verdict is noisy. Count metrics such as HTTP errors are Poisson,
so healthy data produces single-tick spikes that a z-score or robust score flags.
Treating any one verdict as an incident made every healthy run a false positive. This layer looks at verdicts over time and raises an alert only
when enough distinct series are anomalous in the same tick, for enough
consecutive ticks.

The rule reads ``Prediction`` objects and nothing else: their timestamp, series,
and decision. It never sees ground-truth labels, so no label can change an
alert. An alert at tick ``t`` depends only on ticks up to and including ``t``,
so alert times never use later data.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.analytics import SeriesKey
from app.services.detection import Decision, Prediction
from app.services.simulation import DEFAULT_TICK_INTERVAL_SECONDS


@dataclass(frozen=True)
class IncidentRule:
    """Requirements for a run of ticks to raise an alert.

    A tick is hot when at least ``min_affected_series`` distinct series are
    anomalous in it. An alert fires at every tick that extends a run of hot
    ticks to ``min_consecutive_ticks`` or more.

    Consecutive means one expected interval apart, not adjacent in the data.
    ``tick_interval_seconds`` is the telemetry cadence. The default is the
    simulator's documented interval. A streak breaks at any missing tick, and at
    any tick without a hot verdict. An unscored tick has no evidence that the
    series is hot, so it counts as not hot. The default, one series for one
    tick, alerts on any anomalous verdict.
    """

    min_affected_series: int = 1
    min_consecutive_ticks: int = 1
    tick_interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        """Reject requirements that no tick could ever satisfy."""
        if self.min_affected_series < 1:
            raise ValueError("min_affected_series must be at least 1")
        if self.min_consecutive_ticks < 1:
            raise ValueError("min_consecutive_ticks must be at least 1")
        if self.tick_interval_seconds < 1:
            raise ValueError("tick_interval_seconds must be at least 1")

    @property
    def name(self) -> str:
        """Short label for reports, such as ``series>=2 ticks>=3``."""
        return (
            f"series>={self.min_affected_series} "
            f"ticks>={self.min_consecutive_ticks}"
        )

    def alert_times(self, predictions: Iterable[Prediction]) -> list[datetime]:
        """Return the tick timestamps at which an alert fires, in time order.

        ``predictions`` should cover one run. Predictions from several runs
        would share ticks and be counted together. Each alert uses only the
        current tick and the ticks before it, so it is causal.
        """
        anomalous_by_tick: dict[datetime, set[SeriesKey]] = {}
        for prediction in predictions:
            anomalous = anomalous_by_tick.setdefault(prediction.timestamp, set())
            if prediction.decision is Decision.ANOMALOUS:
                anomalous.add(prediction.key)

        interval = timedelta(seconds=self.tick_interval_seconds)
        alerts: list[datetime] = []
        streak = 0
        previous: datetime | None = None
        for tick in sorted(anomalous_by_tick):
            contiguous = previous is not None and tick - previous == interval
            previous = tick
            hot = len(anomalous_by_tick[tick]) >= self.min_affected_series
            if not hot:
                streak = 0
                continue
            streak = streak + 1 if contiguous else 1
            if streak >= self.min_consecutive_ticks:
                alerts.append(tick)
        return alerts


ANY_POSITIVE_RULE = IncidentRule()
