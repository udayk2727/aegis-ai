"""Provider wiring for the compact final answer, with the optional SDKs.

Each adapter sends the compact submit schema, and a compact run through each provider
is verified. The tests use fake clients only. They skip cleanly when an SDK is not
installed, and the provider-free compact tests still run.
"""

import json
from typing import Any

import pytest

pytest.importorskip("anthropic", reason="the optional Anthropic SDK is not installed")
pytest.importorskip("openai", reason="the optional OpenAI SDK is not installed")

from app.domain.agent_records import (  # noqa: E402
    AgentStatus,
    AnswerFormat,
    BudgetLimits,
)
from app.domain.telemetry import ScenarioId  # noqa: E402
from app.services.agent_anthropic_provider import (  # noqa: E402
    COMPACT_SUBMIT_NOTE,
    SUBMIT_TOOL,
    AnthropicMessagesProvider,
)
from app.services.agent_anthropic_provider import (  # noqa: E402
    tool_definitions as anthropic_tools,
)
from app.services.agent_budget import RunBudget  # noqa: E402
from app.services.agent_openai_provider import (  # noqa: E402
    OpenAIResponsesProvider,
)
from app.services.agent_openai_provider import (  # noqa: E402
    tool_definitions as openai_tools,
)
from app.services.agent_orchestration import run_agent  # noqa: E402
from app.services.agent_fake_provider import reference_turns  # noqa: E402
from tests.agent_fixtures import fault_fixture  # noqa: E402
from tests.test_agent_compact_output import _compact  # noqa: E402
from tests.agent_provider_doubles import (  # noqa: E402
    FakeMessagesApi,
    FakeResponsesApi,
    _call,
    _message,
    _response,
    _tool_use,
)

FAULT = fault_fixture(ScenarioId.CPU_SATURATION)
REPORT = FAULT.report
EVENTS = FAULT.events_by_id
MODEL = "claude-sonnet-5-5"
COMPACT = AnswerFormat.COMPACT
FULL = AnswerFormat.FULL


# ----- Provider wiring and schemas -----------------------------------------------


def test_the_default_answer_schema_is_the_full_one() -> None:
    assert anthropic_tools() == anthropic_tools(FULL)
    assert openai_tools() == openai_tools(FULL)


def _answer_schema(definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """The submit tool's schema from a definition list, for either provider."""
    item = next(entry for entry in definitions if entry.get("name") == SUBMIT_TOOL)
    return item["input_schema"] if "input_schema" in item else item["parameters"]


def test_the_compact_schema_names_ids_and_has_no_claims_or_text() -> None:
    schema = _answer_schema(anthropic_tools(COMPACT))

    assert set(schema["properties"]) == {
        "investigation_id",
        "root_cause_assessment",
        "observations",
        "hypotheses",
        "uncertainties",
        "proposed_checks",
    }
    assert "claims" not in schema["properties"]


def test_the_compact_schema_is_smaller_than_the_full_one_for_both_providers() -> None:
    for tools in (anthropic_tools, openai_tools):
        full = json.dumps(_answer_schema(tools(FULL)))
        compact = json.dumps(_answer_schema(tools(COMPACT)))
        assert len(compact) < len(full)


def test_an_anthropic_compact_run_sends_the_compact_schema_and_is_verified() -> None:
    answer = json.loads(_compact().model_dump_json())
    api = FakeMessagesApi(
        [
            _message([_tool_use("list_observations", {}, "toolu_1")]),
            _message([_tool_use(SUBMIT_TOOL, answer, "toolu_2")]),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = AnthropicMessagesProvider(
        api,
        model=MODEL,
        budget=budget,
        sleep=lambda _: None,
        answer_format=COMPACT,
    )

    run = run_agent(REPORT, EVENTS, provider, budget=budget, answer_format=COMPACT)

    assert run.status is AgentStatus.VERIFIED
    assert api.requests[0]["system"].endswith(COMPACT_SUBMIT_NOTE)
    schema = _answer_schema(api.requests[0]["tools"])
    assert "claims" not in schema["properties"]


def test_an_openai_compact_run_is_verified() -> None:
    answer = _compact().model_dump_json()
    api = FakeResponsesApi(
        [
            _response([_call("list_observations", {}, "c1")]),
            _response([_call(SUBMIT_TOOL, answer, "c2")]),
        ]
    )
    budget = RunBudget(BudgetLimits())
    provider = OpenAIResponsesProvider(
        api,
        model="test-model",
        budget=budget,
        answer_format=COMPACT,
    )

    run = run_agent(REPORT, EVENTS, provider, budget=budget, answer_format=COMPACT)

    assert run.status is AgentStatus.VERIFIED
    assert run.claims


def test_a_compact_request_is_smaller_and_its_diagnostics_are_recorded() -> None:
    full_answer = json.loads(reference_turns(REPORT)[-1].final_text or "")
    for claim in full_answer["claims"]:
        claim["text"] = None

    def request_sizes(answer_format: AnswerFormat, answer: Any) -> list[int]:
        """Run the same tool sequence and return each request's serialized size."""
        api = FakeMessagesApi(
            [
                _message([_tool_use("list_observations", {}, "toolu_1")]),
                _message([_tool_use(SUBMIT_TOOL, answer, "toolu_2")]),
            ]
        )
        budget = RunBudget(BudgetLimits())
        provider = AnthropicMessagesProvider(
            api,
            model=MODEL,
            budget=budget,
            sleep=lambda _: None,
            answer_format=answer_format,
        )
        run = run_agent(
            REPORT,
            EVENTS,
            provider,
            budget=budget,
            answer_format=answer_format,
        )
        assert run.accounting is not None
        return [entry.serialized_chars for entry in run.accounting.request_diagnostics]

    full = request_sizes(FULL, full_answer)
    compact = request_sizes(COMPACT, json.loads(_compact().model_dump_json()))

    assert len(full) == len(compact) == 2
    assert compact[0] < full[0]
    assert compact[1] < full[1]
