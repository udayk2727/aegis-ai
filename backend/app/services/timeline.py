"""Deterministic incident timelines: observations and detection decisions, kept apart.

Each entry is either a telemetry observation or a detection decision, and its
``kind`` says which. A decision is the rule firing at a tick, using data up to
and including that tick. An observation is a stored sample. The two are never
merged, so a reader can always tell what was measured from what was concluded.

Entries are ordered by time, then by kind, then by milestone and series, so the
same evidence always gives the same timeline.
"""

from collections.abc import Iterable
from datetime import datetime

from app.domain.incident_records import (
    EvidenceRecord,
    TelemetryGap,
    TimelineEntry,
    TimelineKind,
    TimelineMilestone,
)

_KIND_ORDER: dict[TimelineKind, int] = {
    TimelineKind.OBSERVATION: 0,
    TimelineKind.DETECTION_DECISION: 1,
    TimelineKind.TELEMETRY_GAP: 2,
}


def build_timeline(
    evidence: list[EvidenceRecord],
    first_detection_at: datetime,
    telemetry_gaps: Iterable[TelemetryGap],
) -> list[TimelineEntry]:
    """Return the incident timeline for the alert-contributing evidence, in time order.

    Callers pass only observations taken at alert ticks. Context observations
    would otherwise be presented as the earliest or latest supporting evidence.
    The list must be non-empty and chronological. The first detection is a
    decision, so it appears under its own kind even when it shares a timestamp
    with an observation.
    """
    entries: list[TimelineEntry] = [
        _observation(
            evidence[0],
            TimelineMilestone.EARLIEST_SUPPORTING_OBSERVATION,
        ),
        _observation(
            evidence[-1],
            TimelineMilestone.LATEST_OBSERVED_EVIDENCE,
        ),
    ]

    first_by_series: dict[tuple[str, str], EvidenceRecord] = {}
    for record in evidence:
        series = (record.service, record.metric_name)
        first_by_series.setdefault(series, record)
    entries.extend(
        _observation(record, TimelineMilestone.RELATED_METRIC_CHANGE)
        for record in first_by_series.values()
    )

    entries.append(
        TimelineEntry(
            timestamp=first_detection_at,
            kind=TimelineKind.DETECTION_DECISION,
            milestone=TimelineMilestone.FIRST_QUALIFYING_DETECTION,
            service=None,
            metric_name=None,
            source_event_id=None,
            description=(
                "incident rule first fired at this tick, using data up to and "
                "including this tick"
            ),
        )
    )

    entries.extend(
        TimelineEntry(
            timestamp=gap.observed_after,
            kind=TimelineKind.TELEMETRY_GAP,
            milestone=TimelineMilestone.TELEMETRY_GAP,
            service=None,
            metric_name=None,
            source_event_id=None,
            description=(
                f"no observations between {gap.observed_before.isoformat()} and "
                f"{gap.observed_after.isoformat()}"
            ),
        )
        for gap in telemetry_gaps
    )

    return sorted(
        entries,
        key=lambda entry: (
            entry.timestamp,
            _KIND_ORDER[entry.kind],
            entry.milestone,
            entry.service or "",
            entry.metric_name or "",
        ),
    )


def _observation(
    record: EvidenceRecord,
    milestone: TimelineMilestone,
) -> TimelineEntry:
    """Describe one evidence record as an observation entry."""
    return TimelineEntry(
        timestamp=record.observed_at,
        kind=TimelineKind.OBSERVATION,
        milestone=milestone,
        service=record.service,
        metric_name=record.metric_name,
        source_event_id=record.source_event_id,
        description=(
            f"{record.service} {record.metric_name} observed "
            f"{record.observed_value} {record.unit} ({record.severity})"
        ),
    )
