"""Bounded, thread-safe, in-memory store for simulated telemetry.

Events are kept per run. When adding a run would exceed the event capacity,
the oldest runs are evicted whole, so a run is never partially visible and
its ground-truth labels always match its events. Nothing is persisted; the
store is empty after a restart until the database adapter exists.
"""

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from app.domain.telemetry import MetricName, ScenarioId, ServiceName, TelemetryEvent
from app.services.simulation import SimulationRun

DEFAULT_EVENT_CAPACITY = 50_000


class RunTooLargeError(ValueError):
    """A single run has more events than the whole store can hold.

    Raised before anything is stored, so the store is unchanged. The API maps
    it to a 422 rather than an internal error.
    """

    def __init__(self, event_count: int) -> None:
        """Record how many events the rejected run would have added."""
        super().__init__("run exceeds the event store capacity")
        self.event_count = event_count


@dataclass(frozen=True)
class EventFilter:
    """Optional criteria that narrow the events returned by a query.

    A field set to None places no constraint on that attribute.
    """

    run_id: str | None = None
    scenario_id: ScenarioId | None = None
    service: ServiceName | None = None
    metric_name: MetricName | None = None
    is_anomalous: bool | None = None

    def matches(self, event: TelemetryEvent) -> bool:
        """Return True if the event satisfies every constraint that is set."""
        if self.run_id is not None and event.run_id != self.run_id:
            return False
        if (
            self.scenario_id is not None
            and event.label.scenario_id != self.scenario_id
        ):
            return False
        if self.service is not None and event.service != self.service:
            return False
        if self.metric_name is not None and event.metric_name != self.metric_name:
            return False
        if (
            self.is_anomalous is not None
            and event.label.is_anomalous != self.is_anomalous
        ):
            return False
        return True


class EventStore:
    """Holds simulation runs under a fixed total event capacity."""

    def __init__(self, max_events: int = DEFAULT_EVENT_CAPACITY) -> None:
        """Create an empty store that holds at most ``max_events`` events."""
        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        self._max_events = max_events
        self._runs: OrderedDict[str, SimulationRun] = OrderedDict()
        self._stored_events = 0
        self._lock = Lock()

    def add_run(self, run: SimulationRun) -> bool:
        """Store a run, evicting the oldest runs if needed.

        Returns False without changing the store if a run with the same ID
        is already present. Runs are deterministic, so that copy is identical.
        Raises RunTooLargeError if the run alone exceeds the capacity.
        """
        run_event_count = len(run.events)
        if run_event_count > self._max_events:
            raise RunTooLargeError(run_event_count)
        with self._lock:
            if run.run_id in self._runs:
                return False
            while self._stored_events + run_event_count > self._max_events:
                _, evicted = self._runs.popitem(last=False)
                self._stored_events -= len(evicted.events)
            self._runs[run.run_id] = run
            self._stored_events += run_event_count
            return True

    def matching_events(self, event_filter: EventFilter) -> list[TelemetryEvent]:
        """Return every stored event that matches, in insertion order.

        Used by analytics, which needs whole series rather than a page. The
        result is bounded by the store capacity, so it cannot grow without limit.
        """
        with self._lock:
            return [
                event
                for run in self._runs.values()
                for event in run.events
                if event_filter.matches(event)
            ]

    def runs(self) -> list[SimulationRun]:
        """Return a snapshot of the stored runs, oldest first.

        The list is a copy, so a caller can iterate it while other requests add
        or evict runs. Runs are immutable, so the snapshot cannot change under it.
        """
        with self._lock:
            return list(self._runs.values())

    def query(
        self,
        event_filter: EventFilter,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[TelemetryEvent], int]:
        """Return one page of matching events and the total number of matches.

        Events come back in insertion order: by run, then by tick.
        """
        matches = self.matching_events(event_filter)
        return matches[offset : offset + limit], len(matches)
