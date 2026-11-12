"""Provenance checks that every cited reference in a report resolves to stored events.

The API runs these checks on each report before returning it, so a report that
cites an unknown event, a foreign run, the wrong series, or a time outside the
analysis span is never served. The offline evaluator uses the same function.

This module reads no labels. It is part of the request path and is covered by
the label-isolation test.
"""

from collections.abc import Mapping

from app.domain.investigation_records import Investigation
from app.domain.telemetry import TelemetryEvent


def provenance_violations(
    report: Investigation,
    events_by_id: Mapping[str, TelemetryEvent],
) -> list[str]:
    """Return one message per reference that does not resolve, or none if all do.

    ``events_by_id`` must hold the events of the report's source run. An event
    resolves when it exists, belongs to that run, names one of the series it is
    cited for, and falls inside the analysis span. Each observation and gap must
    also fall inside the span.
    """
    span_start, span_end = report.analysis_span_start, report.analysis_span_end
    problems: list[str] = []
    observations = {item.observation_id: item for item in report.verified_observations}

    for observation in report.verified_observations:
        if observation.first_at < span_start or observation.last_at > span_end:
            problems.append(
                f"{observation.observation_id} lies outside the analysis span"
            )
        series = {(item.service, item.metric_name) for item in observation.series}
        for event_id in observation.source_event_ids:
            event = events_by_id.get(event_id)
            if event is None:
                problems.append(
                    f"{observation.observation_id} cites unknown event {event_id}"
                )
                continue
            if event.run_id != report.source_run_id:
                problems.append(f"{event_id} belongs to another run")
            if (event.service, event.metric_name) not in series:
                problems.append(
                    f"{event_id} is not on a series of {observation.observation_id}"
                )
            if not (
                observation.first_at <= event.timestamp <= observation.last_at
                and span_start <= event.timestamp <= span_end
            ):
                problems.append(f"{event_id} lies outside its observation or the span")

    for hypothesis in report.hypotheses:
        for observation_id in (
            *hypothesis.supporting_observation_ids,
            *hypothesis.contradicting_observation_ids,
        ):
            if observation_id not in observations:
                problems.append(
                    f"{hypothesis.kind.value} cites unknown {observation_id}"
                )
        shown = {
            event_id
            for observation_id in hypothesis.supporting_observation_ids
            if observation_id in observations
            for event_id in observations[observation_id].source_event_ids
        }
        for event_id in hypothesis.supporting_event_ids:
            if event_id not in shown:
                problems.append(
                    f"{hypothesis.kind.value} cites {event_id}, which no supporting "
                    "observation lists"
                )

    for contradiction in report.contradictory_evidence:
        observation = observations.get(contradiction.observation_id)
        if observation is None:
            problems.append(
                f"contradiction cites unknown {contradiction.observation_id}"
            )
            continue
        for event_id in contradiction.source_event_ids:
            if event_id not in observation.source_event_ids:
                problems.append(
                    f"contradiction cites {event_id}, which its observation does "
                    "not list"
                )

    for gap in report.missing_evidence:
        series = {(item.service, item.metric_name) for item in gap.series}
        for event_id in gap.source_event_ids:
            event = events_by_id.get(event_id)
            if event is None:
                problems.append(f"missing evidence cites unknown event {event_id}")
                continue
            if event.run_id != report.source_run_id:
                problems.append(f"{event_id} belongs to another run")
            if series and (event.service, event.metric_name) not in series:
                problems.append(f"{event_id} is not on a series of its gap")
            if not span_start <= event.timestamp <= span_end:
                problems.append(f"{event_id} lies outside the analysis span")

    return problems
