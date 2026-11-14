"""Tests for the allowlisted, read-only tools the investigation agent may call."""

import json

import pytest

from app.domain.agent_records import RecordSource, ToolCall
from app.domain.investigation_records import HypothesisKind
from app.domain.telemetry import ScenarioId
from app.services.agent_tools import (
    REGISTRY,
    UNCERTAINTY_SOURCES,
    canonical_records,
    dispatch,
)
from tests.agent_fixtures import fault_fixture

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report

EXPECTED_TOOLS = {
    "list_observations",
    "get_observation",
    "list_hypotheses",
    "get_hypothesis",
    "list_uncertainties",
    "list_suggested_checks",
}


def _call(name: str, **arguments: object) -> ToolCall:
    """Build a tool request the way a model would send one."""
    return ToolCall(name=name, arguments=dict(arguments))


def test_registry_is_exactly_the_declared_allowlist() -> None:
    assert set(REGISTRY) == EXPECTED_TOOLS


def test_unknown_tool_is_refused_without_echoing_the_request() -> None:
    result = dispatch(
        REPORT,
        _call("shell_run", command="rm -rf /", url="http://example.invalid"),
    )

    assert result.ok is False
    assert result.error == "unknown tool"
    assert result.tool is None
    dumped = result.model_dump_json()
    assert "shell_run" not in dumped
    assert "rm -rf" not in dumped
    assert "example.invalid" not in dumped


@pytest.mark.parametrize(
    "name",
    ["../../read_file", "list_observations; DROP TABLE", "LIST_OBSERVATIONS"],
)
def test_names_outside_the_allowlist_are_refused(name: str) -> None:
    result = dispatch(REPORT, _call(name))

    assert result.ok is False
    assert result.error == "unknown tool"


def test_no_argument_tool_refuses_any_supplied_key() -> None:
    result = dispatch(REPORT, _call("list_hypotheses", url="http://example.invalid"))

    assert result.ok is False
    assert result.error == "invalid arguments"
    assert result.tool == "list_hypotheses"


def test_observation_id_must_match_its_pattern() -> None:
    result = dispatch(REPORT, _call("get_observation", observation_id="../../etc"))

    assert result.ok is False
    assert result.error == "invalid arguments"


def test_observation_outside_this_report_is_not_found() -> None:
    result = dispatch(
        REPORT,
        _call("get_observation", observation_id="obs-anomalous_series-missing-x"),
    )

    assert result.ok is False
    assert result.error == "no matching record in this investigation"


def test_hypothesis_not_generated_for_this_report_is_not_found() -> None:
    generated = {item.kind for item in REPORT.hypotheses}
    absent = next(kind for kind in HypothesisKind if kind not in generated)

    result = dispatch(REPORT, _call("get_hypothesis", kind=absent.value))

    assert result.ok is False
    assert result.error == "no matching record in this investigation"


def test_hypothesis_kind_outside_the_enum_is_refused() -> None:
    result = dispatch(REPORT, _call("get_hypothesis", kind="root_cause_is_db"))

    assert result.ok is False
    assert result.error == "invalid arguments"


def test_list_observations_returns_each_observation_once() -> None:
    result = dispatch(REPORT, _call("list_observations"))

    assert result.ok is True
    assert isinstance(result.data, list)
    ids = [item["record_id"] for item in result.data]
    assert ids == [item.observation_id for item in REPORT.verified_observations]


def test_list_uncertainties_matches_the_canonical_records() -> None:
    result = dispatch(REPORT, _call("list_uncertainties"))
    canonical = {
        (source.value, record_id): text
        for (source, record_id), text in canonical_records(REPORT).items()
        if source in UNCERTAINTY_SOURCES
    }

    assert result.ok is True
    assert isinstance(result.data, list)
    listed = {
        (item["source"], item["record_id"]): item["text"] for item in result.data
    }
    assert listed == canonical


def test_canonical_text_of_each_observation_is_its_statement() -> None:
    records = canonical_records(REPORT)

    for observation in REPORT.verified_observations:
        key = (RecordSource.OBSERVATION, observation.observation_id)
        assert records[key] == observation.statement


def test_every_tool_output_is_free_of_ground_truth_labels() -> None:
    dumped: list[str] = []
    for name in sorted(EXPECTED_TOOLS):
        arguments: dict[str, object] = {}
        if name == "get_observation":
            observation = REPORT.verified_observations[0]
            arguments = {"observation_id": observation.observation_id}
        elif name == "get_hypothesis":
            arguments = {"kind": REPORT.hypotheses[0].kind.value}
        result = dispatch(REPORT, _call(name, **arguments))
        assert result.ok is True, name
        dumped.append(json.dumps(result.model_dump(mode="json")))

    combined = "\n".join(dumped)
    assert '"label"' not in combined
    assert "is_anomalous" not in combined
    assert "scenario_id" not in combined
