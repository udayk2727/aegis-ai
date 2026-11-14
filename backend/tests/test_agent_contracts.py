"""Tests for the agent's Pydantic contracts: shape, forbidden fields, invariants."""

import pytest
from pydantic import ValidationError

from app.domain.agent_records import (
    AgentRun,
    AgentStatus,
    FailureCode,
    ProposedCheck,
    ProviderTurn,
    ToolCall,
    VerificationFailure,
    VerificationStatus,
)
from app.domain.investigation_records import CheckKind, HypothesisKind

INVESTIGATION_ID = "inv-0123456789abcdef"


def test_a_turn_holds_exactly_one_reply() -> None:
    with pytest.raises(ValidationError):
        ProviderTurn()
    with pytest.raises(ValidationError):
        ProviderTurn(tool_call=ToolCall(name="list_hypotheses"), final_text="{}")


def test_a_tool_call_rejects_unexpected_top_level_keys() -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"name": "list_hypotheses", "shell": "rm -rf /"})


def test_a_tool_call_argument_count_is_bounded() -> None:
    arguments = {f"key_{index}": index for index in range(9)}

    with pytest.raises(ValidationError):
        ToolCall(name="list_hypotheses", arguments=arguments)


def test_a_verified_run_must_have_no_failures() -> None:
    failure = VerificationFailure(
        code=FailureCode.UNKNOWN_REFERENCE,
        message="a reference does not resolve",
    )

    with pytest.raises(ValidationError):
        AgentRun(
            investigation_id=INVESTIGATION_ID,
            provider_id="fake",
            status=AgentStatus.VERIFIED,
            verification_status=VerificationStatus.VERIFIED,
            tool_calls=0,
            failures=(failure,),
        )


def test_status_and_verification_must_agree() -> None:
    with pytest.raises(ValidationError):
        AgentRun(
            investigation_id=INVESTIGATION_ID,
            provider_id="fake",
            status=AgentStatus.REJECTED,
            verification_status=VerificationStatus.VERIFIED,
            tool_calls=0,
        )


def test_an_unverified_run_cannot_carry_proposed_checks() -> None:
    check = ProposedCheck(
        kind=CheckKind.REVIEW_QUERY_TRACES,
        affects=(HypothesisKind.CPU_RESOURCE_SATURATION,),
    )

    with pytest.raises(ValidationError):
        AgentRun(
            investigation_id=INVESTIGATION_ID,
            provider_id="fake",
            status=AgentStatus.BUDGET_EXHAUSTED,
            verification_status=VerificationStatus.NOT_VERIFIED,
            tool_calls=3,
            proposed_checks=(check,),
        )
