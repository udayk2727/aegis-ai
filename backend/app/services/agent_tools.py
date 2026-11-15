"""Read-only tools the investigation agent may call, over one investigation report.

The registry is closed. A tool name that is not listed is refused, and each tool
accepts only the arguments its model declares, with extra fields forbidden. Every
tool takes the report as its only context, and no argument names an incident or
a run, so one call cannot reach another incident's data.

Tools read the canonical report and nothing else. They do not read the event
store, ground-truth labels, the file system, or the network, and they return
only records the report already holds.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from app.domain.agent_records import (
    CLAIM_SOURCES,
    ClaimKind,
    RecordSource,
    ToolCall,
    ToolResult,
)
from app.domain.investigation_records import (
    HypothesisKind,
    Investigation,
    ObservationId,
)

RecordKey = tuple[RecordSource, str]

RANKING_CAVEAT_ID = "ranking_caveat"
ERROR_UNKNOWN_TOOL = "unknown tool"
ERROR_INVALID_ARGUMENTS = "invalid arguments"
ERROR_NO_RECORD = "no matching record in this investigation"
UNCERTAINTY_SOURCES = CLAIM_SOURCES[ClaimKind.UNCERTAINTY]


class NoArguments(BaseModel):
    """Arguments for a tool that takes none. Any supplied key is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationQuery(BaseModel):
    """Names one observation of this report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: ObservationId


class HypothesisQuery(BaseModel):
    """Names one hypothesis kind. It must be generated for this report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: HypothesisKind


def canonical_records(report: Investigation) -> dict[RecordKey, str]:
    """Map each citable record to its canonical sentence.

    Keys are ``(source, record_id)``. Indices for missing evidence and limitations
    follow the order the report stores them, so a tool and the verifier agree on
    them. This mapping is the only text a claim may quote.
    """
    records: dict[RecordKey, str] = {}
    for observation in report.verified_observations:
        key = (RecordSource.OBSERVATION, observation.observation_id)
        records[key] = observation.statement
    for hypothesis in report.hypotheses:
        key = (RecordSource.HYPOTHESIS, hypothesis.kind.value)
        records[key] = hypothesis.statement
    for index, gap in enumerate(report.missing_evidence):
        records[(RecordSource.MISSING_EVIDENCE, str(index))] = gap.description
    for index, limitation in enumerate(report.limitations):
        records[(RecordSource.LIMITATION, str(index))] = limitation
    records[(RecordSource.RANKING_CAVEAT, RANKING_CAVEAT_ID)] = report.ranking_caveat
    return records


def _list_observations(report: Investigation, _: NoArguments) -> JsonValue:
    """List each observation with its kind and canonical statement."""
    return [
        {
            "record_id": item.observation_id,
            "kind": item.kind.value,
            "statement": item.statement,
        }
        for item in report.verified_observations
    ]


def _get_observation(report: Investigation, args: ObservationQuery) -> JsonValue:
    """Return one observation with its series, span, and listed event IDs."""
    for item in report.verified_observations:
        if item.observation_id == args.observation_id:
            return item.model_dump(mode="json")
    raise LookupError(args.observation_id)


def _list_hypotheses(report: Investigation, _: NoArguments) -> JsonValue:
    """List each generated hypothesis with its rank, role, and status."""
    fields = {
        "kind",
        "rank",
        "role",
        "status",
        "supporting_observation_ids",
        "contradicting_observation_ids",
    }
    return [item.model_dump(mode="json", include=fields) for item in report.hypotheses]


def _get_hypothesis(report: Investigation, args: HypothesisQuery) -> JsonValue:
    """Return one generated hypothesis in full."""
    for item in report.hypotheses:
        if item.kind is args.kind:
            return item.model_dump(mode="json")
    raise LookupError(args.kind.value)


def _list_uncertainties(report: Investigation, _: NoArguments) -> JsonValue:
    """List the missing evidence, limitations, and ranking caveat with their IDs."""
    return [
        {"source": source.value, "record_id": record_id, "text": text}
        for (source, record_id), text in canonical_records(report).items()
        if source in UNCERTAINTY_SOURCES
    ]


def _list_suggested_checks(report: Investigation, _: NoArguments) -> JsonValue:
    """List the read-only checks the report suggests, with the hypotheses they test."""
    return [
        item.model_dump(mode="json", include={"kind", "description", "affects"})
        for item in report.suggested_checks
    ]


@dataclass(frozen=True)
class ToolSpec:
    """One allowlisted tool: its name, its declared arguments, and its reader."""

    name: str
    description: str
    arguments: type[BaseModel]
    run: Callable[[Investigation, Any], JsonValue]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_observations",
        description="List verified observations with their kind and statement.",
        arguments=NoArguments,
        run=_list_observations,
    ),
    ToolSpec(
        name="get_observation",
        description="Return one observation with its series and cited event IDs.",
        arguments=ObservationQuery,
        run=_get_observation,
    ),
    ToolSpec(
        name="list_hypotheses",
        description="List generated hypotheses with rank, role, and status.",
        arguments=NoArguments,
        run=_list_hypotheses,
    ),
    ToolSpec(
        name="get_hypothesis",
        description="Return one generated hypothesis in full.",
        arguments=HypothesisQuery,
        run=_get_hypothesis,
    ),
    ToolSpec(
        name="list_uncertainties",
        description="List missing evidence, limitations, and the ranking caveat.",
        arguments=NoArguments,
        run=_list_uncertainties,
    ),
    ToolSpec(
        name="list_suggested_checks",
        description="List the read-only checks the report suggests.",
        arguments=NoArguments,
        run=_list_suggested_checks,
    ),
)

REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def dispatch(report: Investigation, call: ToolCall) -> ToolResult:
    """Validate a model's tool request and run it, or return a fixed refusal.

    An unknown name, invalid arguments, or a record this report does not hold
    each return ``ok=False`` with a fixed message. The model's own name and
    arguments are never echoed back.
    """
    spec = REGISTRY.get(call.name)
    if spec is None:
        return ToolResult(ok=False, error=ERROR_UNKNOWN_TOOL)
    try:
        args = spec.arguments.model_validate(call.arguments)
    except ValidationError:
        return ToolResult(ok=False, tool=spec.name, error=ERROR_INVALID_ARGUMENTS)
    try:
        data = spec.run(report, args)
    except LookupError:
        return ToolResult(ok=False, tool=spec.name, error=ERROR_NO_RECORD)
    return ToolResult(ok=True, tool=spec.name, data=data)
