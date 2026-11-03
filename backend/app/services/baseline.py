"""Normal-operation baselines and a leakage-safe train/evaluation split.

A baseline is the descriptive statistics of events that are labelled normal.
Ground-truth labels are used here, and only here, to choose which events
describe normal operation. Baselines carry no labels, and nothing in this
module feeds a label to a detector as a feature.

The split guards against leakage in two ways. Training and evaluation use
disjoint simulation runs, so a model never scores data it was fitted on. Training
data also ends before a cutoff time and evaluation data starts at or after it,
so no evaluation point precedes a training point.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.telemetry import TelemetryEvent
from app.services.analytics import SeriesSummary, summarize

SELECTION_DESCRIPTION = (
    "events whose ground-truth label is not anomalous, from the selected runs; "
    "when a cutoff is given, only events before it"
)


@dataclass(frozen=True)
class Baseline:
    """Normal-operation statistics and the provenance needed to reproduce them."""

    sample_count: int
    source_run_ids: tuple[str, ...]
    series: tuple[SeriesSummary, ...]


@dataclass(frozen=True)
class TrainEvaluationSplit:
    """Disjoint training and evaluation runs with a time cutoff between them.

    Training events must be normal-labelled, from a training run, and strictly
    before the cutoff. Evaluation events come from an evaluation run and are at
    or after the cutoff. The run sets must not overlap.
    """

    training_run_ids: frozenset[str]
    evaluation_run_ids: frozenset[str]
    cutoff: datetime

    def __post_init__(self) -> None:
        """Reject overlapping runs and a cutoff that is not a UTC instant."""
        if not self.training_run_ids:
            raise ValueError("at least one training run is required")
        if not self.evaluation_run_ids:
            raise ValueError("at least one evaluation run is required")
        if self.training_run_ids & self.evaluation_run_ids:
            raise ValueError("training and evaluation runs must not overlap")
        offset = self.cutoff.utcoffset()
        if offset is None:
            raise ValueError("cutoff must be timezone-aware")
        if offset != timedelta(0):
            raise ValueError("cutoff must be expressed in UTC")

    def training_events(
        self,
        events: Iterable[TelemetryEvent],
    ) -> list[TelemetryEvent]:
        """Return normal-labelled events from training runs before the cutoff."""
        return [
            event
            for event in events
            if event.run_id in self.training_run_ids
            and event.timestamp < self.cutoff
            and not event.label.is_anomalous
        ]

    def training_baseline(self, events: Iterable[TelemetryEvent]) -> Baseline:
        """Build the baseline an evaluation should be scored against.

        Only the training side of the split contributes. Evaluation runs and
        events after the cutoff are dropped before any statistic is computed,
        so a held-out run cannot move the reference it is compared with. Use
        this, not build_baseline, in any train-then-evaluate workflow.
        """
        return build_baseline(self.training_events(events))

    def evaluation_events(
        self,
        events: Iterable[TelemetryEvent],
    ) -> list[TelemetryEvent]:
        """Return events from evaluation runs at or after the cutoff.

        Labels are kept here because scoring a detector needs ground truth. They
        are never used to build a feature.
        """
        return [
            event
            for event in events
            if event.run_id in self.evaluation_run_ids
            and event.timestamp >= self.cutoff
        ]


def build_baseline(events: Iterable[TelemetryEvent]) -> Baseline:
    """Build a baseline from the normal-labelled events among ``events``.

    Anomalous events are dropped before any statistic is computed. Source run
    IDs record exactly which runs contributed, so the baseline can be traced.
    """
    normal = [event for event in events if not event.label.is_anomalous]
    source_run_ids = tuple(sorted({event.run_id for event in normal}))
    return Baseline(
        sample_count=len(normal),
        source_run_ids=source_run_ids,
        series=tuple(summarize(normal)),
    )
