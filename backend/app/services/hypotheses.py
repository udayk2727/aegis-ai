"""Hypothesis generation: fixed rules applied to verified observations only.

A rule produces a hypothesis when two conditions hold. Its primary signal must
have contributed to alerts at no fewer than ``MIN_PRIMARY_ALERT_TICKS`` ticks in
the span, and each competing signal must be measured normal. Then the rule
reads its corroborating signals and its ordering:

- An anomalous corroborating signal is supporting evidence.
- A corroborating signal measured normal is contradicting evidence, because the
  hypothesis expected it to move.
- An ordering that runs backwards is contradicting evidence. The expected-first
  signal must not start after the expected-second one.

The engine reads observations and nothing else. It never sees raw events, so it
cannot be tuned to a label, and it cannot cite an event that no observation
names.

Ranking orders candidates by fewer contradicting observations, then by more
anomalous supporting observations, then by catalog order. It is an ordering aid,
not a probability. A count of observations is not a measure of how likely a
cause is.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.investigation_records import (
    ContradictoryEvidence,
    Hypothesis,
    HypothesisKind,
    HypothesisStatus,
    MissingEvidence,
    MissingEvidenceKind,
    ObservationKind,
    SeriesRef,
    VerificationStatus,
    VerifiedObservation,
)
from app.services.investigation_catalog import (
    MIN_PRIMARY_ALERT_TICKS,
    RULES,
    HypothesisRule,
)


@dataclass(frozen=True)
class HypothesisAssessment:
    """Generated hypotheses, the contradictions behind them, and what was missing."""

    hypotheses: tuple[Hypothesis, ...]
    contradictions: tuple[ContradictoryEvidence, ...]
    missing: tuple[MissingEvidence, ...]


@dataclass(frozen=True)
class _Candidate:
    """One generated hypothesis before it is ranked."""

    rule: HypothesisRule
    supporting: tuple[str, ...]
    contradicting: tuple[str, ...]
    anomalous_support: int
    services: tuple[str, ...]
    event_ids: tuple[str, ...]


def assess_hypotheses(
    observations: Sequence[VerifiedObservation],
    rules: Sequence[HypothesisRule] = RULES,
) -> HypothesisAssessment:
    """Apply each rule to the observations and return the ranked assessment.

    With no observations, nothing is generated and every rule reports why. The
    result depends only on the observations and the rules, not on their order.
    """
    by_id = {item.observation_id: item for item in observations}
    anomalous = _by_series(observations, ObservationKind.ANOMALOUS_SERIES)
    normal = _by_series(observations, ObservationKind.NORMAL_SERIES)
    onsets = {
        (item.series[0], item.series[1]): item
        for item in observations
        if item.kind is ObservationKind.ONSET_ORDER
    }

    missing: list[MissingEvidence] = []
    contradictions: list[ContradictoryEvidence] = []
    candidates: list[_Candidate] = []
    for rule in rules:
        primary_ticks = (
            len(anomalous[rule.primary].source_event_ids)
            if rule.primary in anomalous
            else 0
        )
        if primary_ticks < MIN_PRIMARY_ALERT_TICKS:
            missing.append(
                MissingEvidence(
                    kind=MissingEvidenceKind.PRIMARY_SIGNAL_NOT_SUSTAINED,
                    description=(
                        f"{_name(rule.primary)} contributed to alerts at "
                        f"{primary_ticks} tick(s) in the span; {rule.kind.value} "
                        f"needs at least {MIN_PRIMARY_ALERT_TICKS}, so it was not "
                        "generated."
                    ),
                    series=(rule.primary,),
                    affects=(rule.kind,),
                )
            )
            continue
        unruled = [series for series in rule.competing if series not in normal]
        if unruled:
            missing.append(
                MissingEvidence(
                    kind=MissingEvidenceKind.COMPETING_SIGNAL_NOT_RULED_OUT,
                    description=(
                        f"{_name(unruled[0])} is not measured normal in the span, so "
                        f"{rule.kind.value} was not generated."
                    ),
                    series=tuple(unruled),
                    affects=(rule.kind,),
                )
            )
            continue

        supporting = [anomalous[rule.primary].observation_id]
        contradicting: list[str] = []
        for series in rule.corroborating:
            if series in anomalous:
                supporting.append(anomalous[series].observation_id)
            elif series in normal:
                observation = normal[series]
                contradicting.append(observation.observation_id)
                contradictions.append(
                    ContradictoryEvidence(
                        hypothesis_kind=rule.kind,
                        observation_id=observation.observation_id,
                        explanation=(
                            f"{_name(series)} did not contribute to an alert in the "
                            "span, but this hypothesis expects it to."
                        ),
                        source_event_ids=observation.source_event_ids,
                        source_event_count=len(observation.source_event_ids),
                    )
                )
            else:
                missing.append(
                    MissingEvidence(
                        kind=MissingEvidenceKind.CORROBORATION_NOT_OBSERVED,
                        description=(
                            f"{_name(series)} has no verdict in the span, so it "
                            f"cannot support or contradict {rule.kind.value}."
                        ),
                        series=(series,),
                        affects=(rule.kind,),
                    )
                )
        if rule.ordering is not None and rule.ordering in onsets:
            earlier, later = rule.ordering
            onset = onsets[rule.ordering]
            if anomalous[earlier].first_at <= anomalous[later].first_at:
                supporting.append(onset.observation_id)
            else:
                contradicting.append(onset.observation_id)
                contradictions.append(
                    ContradictoryEvidence(
                        hypothesis_kind=rule.kind,
                        observation_id=onset.observation_id,
                        explanation=(
                            f"{_name(later)} first contributed to an alert before "
                            f"{_name(earlier)}, which this hypothesis expects first."
                        ),
                        source_event_ids=onset.source_event_ids,
                        source_event_count=len(onset.source_event_ids),
                    )
                )
        supporting.extend(normal[series].observation_id for series in rule.competing)

        supporting_ids = tuple(dict.fromkeys(supporting))
        candidates.append(
            _Candidate(
                rule=rule,
                supporting=supporting_ids,
                contradicting=tuple(dict.fromkeys(contradicting)),
                anomalous_support=sum(
                    1
                    for observation_id in supporting_ids
                    if by_id[observation_id].kind is not ObservationKind.NORMAL_SERIES
                ),
                services=tuple(sorted({series.service for series in rule.series})),
                event_ids=tuple(
                    sorted(
                        {
                            event_id
                            for observation_id in supporting_ids
                            for event_id in by_id[observation_id].source_event_ids
                        }
                    )
                ),
            )
        )

    catalog_index = {rule.kind: index for index, rule in enumerate(rules)}
    ranked = sorted(
        candidates,
        key=lambda item: (
            len(item.contradicting),
            -item.anomalous_support,
            catalog_index[item.rule.kind],
        ),
    )
    kinds = [candidate.rule.kind for candidate in ranked]
    hypotheses = tuple(
        _hypothesis(
            candidate,
            rank,
            [kind for kind in kinds if kind != candidate.rule.kind],
        )
        for rank, candidate in enumerate(ranked, start=1)
    )
    ranked_contradictions = tuple(
        contradiction
        for candidate in ranked
        for contradiction in contradictions
        if contradiction.hypothesis_kind is candidate.rule.kind
    )
    return HypothesisAssessment(
        hypotheses=hypotheses,
        contradictions=ranked_contradictions,
        missing=tuple(missing),
    )


def _hypothesis(
    candidate: _Candidate,
    rank: int,
    alternatives: Sequence[HypothesisKind],
) -> Hypothesis:
    """Build the public hypothesis record for one ranked candidate."""
    status = (
        HypothesisStatus.SUPPORTED_WITH_CONTRADICTIONS
        if candidate.contradicting
        else HypothesisStatus.SUPPORTED
    )
    return Hypothesis(
        kind=candidate.rule.kind,
        rank=rank,
        role=candidate.rule.role,
        statement=candidate.rule.statement,
        status=status,
        verification_status=VerificationStatus.UNVERIFIED,
        affected_services=candidate.services,
        supporting_observation_ids=candidate.supporting,
        contradicting_observation_ids=candidate.contradicting,
        supporting_event_ids=candidate.event_ids,
        supporting_event_count=len(candidate.event_ids),
        alternative_kinds=tuple(alternatives),
        uncertainty=candidate.rule.uncertainty,
    )


def _by_series(
    observations: Sequence[VerifiedObservation],
    kind: ObservationKind,
) -> dict[SeriesRef, VerifiedObservation]:
    """Index single-series observations of one kind by their series."""
    return {item.series[0]: item for item in observations if item.kind is kind}


def _name(series: SeriesRef) -> str:
    """Name a series the way observations do, as service and metric."""
    return f"{series.metric_name.value} on {series.service}"
