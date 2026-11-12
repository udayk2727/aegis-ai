"""Investigation workflow: one incident and its run's verdicts in, one report out.

Read-only and offline. The function takes an incident and the detector's verdicts
for that incident's source run, and it returns a report. It performs no I/O and
reads no labels, and the same inputs always give the same report.

Steps:

1. Build verified observations from the incident's evidence and the verdicts.
2. Apply the fixed hypothesis rules to those observations.
3. Record what could not be checked: sources the telemetry model does not
   collect, competing signals that were not ruled out, unscored measurements,
   and series with no measurements in the run.
4. Suggest read-only checks for the generated hypotheses.

Nothing in the report is a root-cause finding. The report says what was
observed, which patterns it is consistent with, and what would be needed to test
them.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from app.domain.incident_records import Incident, RootCauseAssessment
from app.domain.investigation_records import (
    CheckKind,
    ContradictoryEvidence,
    Hypothesis,
    HypothesisKind,
    HypothesisRole,
    Investigation,
    InvestigationId,
    InvestigationStatus,
    MissingEvidence,
    MissingEvidenceKind,
    SeriesRef,
    SuggestedCheck,
    VerifiedObservation,
)
from app.services.detection import Decision, Prediction
from app.services.hypotheses import assess_hypotheses
from app.services.investigation_catalog import (
    CHECK_DESCRIPTIONS,
    DB_CONNECTIONS,
    DIRECTION_NOTE,
    ORDERINGS,
    RULES,
    SOURCE_GAPS,
    WATCHED_SERIES,
)
from app.services.investigation_observations import (
    analysis_span,
    build_observations,
    series_of_prediction,
)

ANALYSIS_VERSION = "day6-investigation-v1"

RANKING_BASIS = (
    "Hypotheses are ordered by fewer contradicting observations, then by more "
    "anomalous supporting observations, then by fixed catalog order. This orders "
    "the candidates for review. It is not a probability or a confidence score."
)

RANKING_CAVEAT = (
    "The order reflects heuristic evidence support: how many observations each "
    "rule matched once its prerequisites held. It favours hypotheses with more "
    "corroborating series. It is not a measure of causal certainty, and no "
    "hypothesis carries a probability. Read each hypothesis's role and its "
    "alternatives before treating rank 1 as the likely starting point. Every "
    "hypothesis stays unverified."
)

MAX_CITED_EVENT_IDS = 100
"""Default cap on event IDs listed per cited evidence list in one report.

Totals are kept next to each capped list, so a truncated list still shows how
many events it refers to. The default is the API default, and the setting can
be changed in configuration.
"""

LIMITATIONS: tuple[str, ...] = (
    "Hypotheses are produced by fixed rules over a small set of simulated "
    "signals. They are not validated causal explanations.",
    "Timing and co-occurrence do not establish cause. Temporal precedence is "
    "necessary for a causal reading, but it is not sufficient.",
    "Each rule states what the data shows. A hypothesis is never verified by the "
    "analysis, and its verification status stays unverified.",
    "Evidence comes from the simulator. Its faults are large step changes, so "
    "production telemetry will be less clean.",
    "The baseline is the fault-free runs named in the request. It is not a "
    "production baseline, and it assumes stationary normal behaviour.",
    "Query-level traces, application logs, host metrics, pool limits, and change "
    "records are not in the telemetry model, so they cannot be checked here.",
)


def investigate(
    incident: Incident,
    predictions: Sequence[Prediction],
    *,
    max_cited_event_ids: int = MAX_CITED_EVENT_IDS,
) -> Investigation:
    """Build the investigation report for one incident from its source-run verdicts.

    ``predictions`` must cover the source run, as returned by ``score_run``.
    Normal observations need them, so a report built from a partial list may
    treat a normal series as missing. Each cited event list is capped at
    ``max_cited_event_ids``. The cap changes what is listed, never what is
    counted, and the ranking is computed before the cap is applied.
    """
    if max_cited_event_ids < 1:
        raise ValueError("max_cited_event_ids must be at least 1")
    span_start, span_end = analysis_span(incident)
    observations = build_observations(
        incident,
        predictions,
        watched=WATCHED_SERIES,
        orderings=ORDERINGS,
    )
    assessment = assess_hypotheses(observations)
    generated = {hypothesis.kind for hypothesis in assessment.hypotheses}
    missing = [
        *assessment.missing,
        *_source_gaps(generated),
        *_measurement_gaps(predictions, span_start, span_end),
        *_direction_note(generated),
    ]
    bounded_observations = [
        _bound_observation(item, max_cited_event_ids) for item in observations
    ]
    bounded_contradictions = [
        _bound_contradiction(item, max_cited_event_ids)
        for item in assessment.contradictions
    ]
    bounded_missing = [_bound_missing(item, max_cited_event_ids) for item in missing]
    visible = {
        observation.observation_id: set(observation.source_event_ids)
        for observation in bounded_observations
    }
    bounded_hypotheses = [
        _bound_hypothesis(item, visible, max_cited_event_ids)
        for item in assessment.hypotheses
    ]
    return Investigation(
        investigation_id=_investigation_id(incident.incident_id),
        analysis_version=ANALYSIS_VERSION,
        source_incident_id=incident.incident_id,
        source_run_id=incident.source_run_id,
        baseline_run_ids=incident.baseline_run_ids,
        analysis_span_start=span_start,
        analysis_span_end=span_end,
        status=(
            InvestigationStatus.ANALYZED
            if generated
            else InvestigationStatus.INSUFFICIENT_EVIDENCE
        ),
        root_cause_assessment=RootCauseAssessment.NOT_ASSESSED,
        ranking_basis=RANKING_BASIS,
        ranking_caveat=_ranking_caveat(assessment.hypotheses),
        verified_observations=tuple(bounded_observations),
        hypotheses=tuple(bounded_hypotheses),
        contradictory_evidence=tuple(bounded_contradictions),
        missing_evidence=tuple(bounded_missing),
        evidence_truncated=(
            any(item.source_events_truncated for item in bounded_observations)
            or any(item.supporting_events_truncated for item in bounded_hypotheses)
            or any(item.source_events_truncated for item in bounded_contradictions)
            or any(item.source_events_truncated for item in bounded_missing)
        ),
        suggested_checks=_suggested_checks(generated, missing),
        limitations=LIMITATIONS,
    )


def _investigation_id(incident_id: str) -> InvestigationId:
    """Derive a stable ID from the incident and the analysis version."""
    payload = json.dumps(
        {"incident_id": incident_id, "analysis_version": ANALYSIS_VERSION},
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"inv-{digest[:16]}"


def _affects(series: SeriesRef) -> tuple[HypothesisKind, ...]:
    """Return the hypothesis kinds whose rules read a series, in catalog order."""
    return tuple(rule.kind for rule in RULES if series in rule.series)


def _source_gaps(generated: set[HypothesisKind]) -> list[MissingEvidence]:
    """Report uncollected sources that bear on a generated hypothesis."""
    gaps: list[MissingEvidence] = []
    for gap in SOURCE_GAPS:
        affected = tuple(kind for kind in gap.affects if kind in generated)
        if affected:
            gaps.append(
                MissingEvidence(
                    kind=MissingEvidenceKind.NOT_IN_TELEMETRY_MODEL,
                    description=gap.description,
                    affects=affected,
                )
            )
    return gaps


def _measurement_gaps(
    predictions: Sequence[Prediction],
    span_start: datetime,
    span_end: datetime,
) -> list[MissingEvidence]:
    """Report unmeasured series, and unscored measurements in the span."""
    gaps: list[MissingEvidence] = []
    for series in WATCHED_SERIES:
        measured = [
            prediction
            for prediction in predictions
            if series_of_prediction(prediction) == series
        ]
        if not measured:
            gaps.append(
                MissingEvidence(
                    kind=MissingEvidenceKind.NO_MEASUREMENT_IN_RUN,
                    description=(
                        f"{series.metric_name.value} on {series.service} has no "
                        "measurements in the source run."
                    ),
                    series=(series,),
                    affects=_affects(series),
                )
            )
            continue
        unscored = sorted(
            (
                prediction
                for prediction in measured
                if prediction.decision is Decision.UNSCORED
                and span_start <= prediction.timestamp <= span_end
            ),
            key=lambda prediction: prediction.timestamp,
        )
        if unscored:
            gaps.append(
                MissingEvidence(
                    kind=MissingEvidenceKind.UNSCORED_MEASUREMENTS,
                    description=(
                        f"{len(unscored)} measurement(s) of "
                        f"{series.metric_name.value} on {series.service} in the span "
                        "were unscored, so they cannot show normal or anomalous."
                    ),
                    series=(series,),
                    source_event_ids=tuple(item.event_id for item in unscored),
                    source_event_count=len(unscored),
                    affects=_affects(series),
                )
            )
    return gaps


def _direction_note(generated: set[HypothesisKind]) -> list[MissingEvidence]:
    """Record that the detector cannot see downward connection movement."""
    if HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM not in generated:
        return []
    return [
        MissingEvidence(
            kind=MissingEvidenceKind.DIRECTION_NOT_EVALUATED,
            description=DIRECTION_NOTE,
            series=(DB_CONNECTIONS,),
            affects=(HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM,),
        )
    ]


def _suggested_checks(
    generated: set[HypothesisKind],
    missing: Sequence[MissingEvidence],
) -> tuple[SuggestedCheck, ...]:
    """Suggest the read-only checks that bear on the generated hypotheses."""
    requests: dict[CheckKind, set[HypothesisKind]] = defaultdict(set)
    for gap in SOURCE_GAPS:
        for kind in gap.affects:
            if kind in generated:
                requests[gap.check].add(kind)
    if HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM in generated:
        requests[CheckKind.RESCORE_DOWNWARD_DEVIATIONS].add(
            HypothesisKind.DATABASE_CONNECTIVITY_PROBLEM
        )
    for item in missing:
        if item.kind is MissingEvidenceKind.UNSCORED_MEASUREMENTS:
            for kind in item.affects:
                if kind in generated:
                    requests[CheckKind.REVIEW_UNSCORED_MEASUREMENTS].add(kind)

    kind_order = {rule.kind: index for index, rule in enumerate(RULES)}
    check_order = list(CheckKind)
    return tuple(
        SuggestedCheck(
            kind=check,
            description=CHECK_DESCRIPTIONS[check],
            affects=tuple(sorted(requests[check], key=kind_order.__getitem__)),
        )
        for check in sorted(requests, key=check_order.index)
    )


def _ranking_caveat(hypotheses: Sequence[Hypothesis]) -> str:
    """State the ranking limits, and name a downstream symptom holding rank 1.

    When the top hypothesis is a downstream symptom and a potentially initiating
    fault was also generated, the caveat names both, so the symptom is not read
    as the starting point.
    """
    initiators = [
        hypothesis.kind.value
        for hypothesis in hypotheses
        if hypothesis.role is HypothesisRole.POTENTIAL_INITIATING_FAULT
    ]
    if not hypotheses or hypotheses[0].role is not HypothesisRole.DOWNSTREAM_SYMPTOM:
        return RANKING_CAVEAT
    if not initiators:
        return (
            f"{RANKING_CAVEAT} Rank 1, {hypotheses[0].kind.value}, is a downstream "
            "symptom, and no potentially initiating hypothesis was generated."
        )
    return (
        f"{RANKING_CAVEAT} Rank 1, {hypotheses[0].kind.value}, is a downstream "
        "symptom. Potentially initiating hypotheses were also generated: "
        f"{', '.join(initiators)}."
    )


def _bound_observation(
    observation: VerifiedObservation,
    limit: int,
) -> VerifiedObservation:
    """Keep the first ``limit`` cited events of an observation. The total stays."""
    kept = observation.source_event_ids[:limit]
    return observation.model_copy(
        update={
            "source_event_ids": kept,
            "source_events_truncated": len(kept) < observation.source_event_count,
        }
    )


def _bound_hypothesis(
    hypothesis: Hypothesis,
    visible: dict[str, set[str]],
    limit: int,
) -> Hypothesis:
    """Keep supporting events that a listed observation still shows, up to the cap.

    Only events that appear in the bounded observations are kept, so every
    listed hypothesis event resolves to a listed observation.
    """
    shown = set().union(
        *(
            visible[observation_id]
            for observation_id in hypothesis.supporting_observation_ids
        )
    )
    kept = tuple(
        event_id for event_id in hypothesis.supporting_event_ids if event_id in shown
    )[:limit]
    return hypothesis.model_copy(
        update={
            "supporting_event_ids": kept,
            "supporting_events_truncated": (
                len(kept) < hypothesis.supporting_event_count
            ),
        }
    )


def _bound_contradiction(
    contradiction: ContradictoryEvidence,
    limit: int,
) -> ContradictoryEvidence:
    """Keep the first ``limit`` events of a contradiction. The total stays."""
    kept = contradiction.source_event_ids[:limit]
    return contradiction.model_copy(
        update={
            "source_event_ids": kept,
            "source_events_truncated": len(kept) < contradiction.source_event_count,
        }
    )


def _bound_missing(item: MissingEvidence, limit: int) -> MissingEvidence:
    """Keep the first ``limit`` event IDs of a gap. The total stays."""
    if not item.source_event_ids:
        return item
    kept = item.source_event_ids[:limit]
    return item.model_copy(
        update={
            "source_event_ids": kept,
            "source_events_truncated": len(kept) < item.source_event_count,
        }
    )
