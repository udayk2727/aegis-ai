"""Agent records: the contracts between an investigation, a model, and the verifier.

A model is untrusted. Its turns and its final answer are parsed into these
models, and nothing it says reaches a caller until the verifier accepts it. Every
model forbids extra fields, so an unexpected key is a refusal, not an instruction
that gets silently ignored.

A final answer is a list of claims. Each claim selects one record of the
deterministic report by its ID. The server then supplies that record's canonical
sentence. If a model does supply text, the verifier requires it to match the
canonical sentence word for word, so a claim cannot add an assertion the report
does not make. Free-form narrative is out of scope.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from app.domain.investigation_records import (
    CheckKind,
    HypothesisKind,
    InvestigationId,
    ObservationId,
)
from app.domain.telemetry import EventId

MAX_CLAIMS = 256
MAX_CLAIM_CHARS = 2_000
MAX_REFERENCE_EVENTS = 1_000
MAX_MESSAGE_CHARS = 256_000
MAX_TOOL_NAME_CHARS = 64
MAX_TOOL_ARGUMENTS = 8
MAX_RECORD_ID_CHARS = 200
PROVIDER_REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
MAX_CALL_ID_CHARS = 128
MAX_REPLAY_BLOCKS = 16


class MessageRole(StrEnum):
    """Who wrote a transcript message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TokenUsage(BaseModel):
    """Tokens a provider reported. Counts are never negative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0

    @property
    def total(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def plus(self, other: "TokenUsage | None") -> "TokenUsage":
        """Return this usage with another reply's usage added to it."""
        if other is None:
            return self
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class BudgetStop(StrEnum):
    """The limit that ended a run early. Each one is checked before it is passed."""

    REQUESTS = "requests"
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    DEADLINE = "deadline"


class BudgetLimits(BaseModel):
    """The limits on one run. Defaults are conservative, and each has a hard maximum.

    A local timeout does not cancel a request on the server, and the provider may
    still bill a request that timed out. The limits bound what this code asks for,
    not what the provider charges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_requests: Annotated[int, Field(ge=1, le=20)] = 8
    max_tool_calls: Annotated[int, Field(ge=0, le=20)] = 6
    max_total_tokens: Annotated[int, Field(ge=1, le=200_000)] = 30_000
    max_output_tokens: Annotated[int, Field(ge=1, le=4_000)] = 1_000
    request_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    deadline_seconds: Annotated[float, Field(gt=0, le=900)] = 180.0
    max_retries: Annotated[int, Field(ge=0, le=3)] = 0


class ProviderFailureReason(StrEnum):
    """Why a provider did not produce a usable turn. Carries no provider text."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    API_ERROR = "api_error"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"
    EXHAUSTED = "exhausted"
    BUDGET = "budget"


class ProviderMessage(BaseModel):
    """One message in the transcript a model reads on each turn.

    ``call_id`` ties a tool request to its result. A provider that does not
    number its tool calls leaves it unset. ``replay_blocks`` holds the provider's
    own assistant content for that request, in its original order, when the
    provider needs it replayed exactly (for example, thinking blocks with their
    signatures). It is opaque to the agent, and it is never part of a result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: MessageRole
    content: str = Field(max_length=MAX_MESSAGE_CHARS)
    call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CALL_ID_CHARS,
    )
    replay_blocks: tuple[dict[str, JsonValue], ...] = Field(
        default=(),
        max_length=MAX_REPLAY_BLOCKS,
        repr=False,
    )

    @model_validator(mode="after")
    def _replay_only_for_tool_requests(self) -> Self:
        """Allow replay blocks only on an assistant tool request with a call ID."""
        if self.replay_blocks and (
            self.role is not MessageRole.ASSISTANT or self.call_id is None
        ):
            raise ValueError("replay blocks belong to an assistant tool request")
        return self


class ToolCall(BaseModel):
    """A model's request to run one tool. The name and arguments are untrusted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=MAX_TOOL_NAME_CHARS)
    arguments: dict[str, JsonValue] = Field(
        default_factory=dict,
        max_length=MAX_TOOL_ARGUMENTS,
    )
    call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CALL_ID_CHARS,
    )


class ProviderTurn(BaseModel):
    """One model reply: either a tool request or a final answer, never both.

    ``usage`` is the reply's token count when the provider reports one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call: ToolCall | None = None
    final_text: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    usage: TokenUsage | None = None
    replay_blocks: tuple[dict[str, JsonValue], ...] = Field(
        default=(),
        max_length=MAX_REPLAY_BLOCKS,
        repr=False,
    )

    @model_validator(mode="after")
    def _exactly_one_reply(self) -> Self:
        """Require exactly one of a tool request or a final answer."""
        if (self.tool_call is None) == (self.final_text is None):
            raise ValueError(
                "a turn holds exactly one of a tool call or a final answer"
            )
        if self.replay_blocks and self.tool_call is None:
            raise ValueError("replay blocks belong to a tool request")
        return self


class ToolResult(BaseModel):
    """The outcome of one tool request, sent back to the model as data.

    Error text is fixed. It never repeats the model's name or arguments, so a
    refused request cannot echo an injected instruction back into the transcript.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    tool: str | None = None
    data: JsonValue | None = None
    error: str | None = None


class RecordSource(StrEnum):
    """The kind of canonical record a reference points at in the report."""

    OBSERVATION = "observation"
    HYPOTHESIS = "hypothesis"
    MISSING_EVIDENCE = "missing_evidence"
    LIMITATION = "limitation"
    RANKING_CAVEAT = "ranking_caveat"


class ClaimKind(StrEnum):
    """What a claim is about. Each kind may cite only certain record sources.

    ``observation`` is a verified observation. ``hypothesis`` is an existing,
    unconfirmed root-cause candidate. ``uncertainty`` is missing evidence, a
    limitation, or the ranking caveat.
    """

    OBSERVATION = "observation"
    HYPOTHESIS = "hypothesis"
    UNCERTAINTY = "uncertainty"


CLAIM_SOURCES: dict[ClaimKind, frozenset[RecordSource]] = {
    ClaimKind.OBSERVATION: frozenset({RecordSource.OBSERVATION}),
    ClaimKind.HYPOTHESIS: frozenset({RecordSource.HYPOTHESIS}),
    ClaimKind.UNCERTAINTY: frozenset(
        {
            RecordSource.MISSING_EVIDENCE,
            RecordSource.LIMITATION,
            RecordSource.RANKING_CAVEAT,
        }
    ),
}
"""Which record sources each claim kind may cite. Enforced by the verifier."""


class EvidenceReference(BaseModel):
    """The record a claim relies on, bound to one investigation.

    ``record_id`` is an observation ID, a hypothesis kind, or a short index for
    the records that have none. ``event_ids`` is allowed only on an observation
    reference, and each ID must be one the observation lists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: InvestigationId
    source: RecordSource
    record_id: str = Field(min_length=1, max_length=MAX_RECORD_ID_CHARS)
    event_ids: tuple[EventId, ...] = Field(default=(), max_length=MAX_REFERENCE_EVENTS)


class AgentClaim(BaseModel):
    """One statement tied to one record of the report.

    ``text`` is optional. When it is left out, the server fills it from the
    record's canonical text once the claim is accepted. When a model supplies it,
    the verifier requires it to equal that canonical text exactly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ClaimKind
    text: str | None = Field(default=None, min_length=1, max_length=MAX_CLAIM_CHARS)
    reference: EvidenceReference


class ProposedCheck(BaseModel):
    """A read-only check the report already suggests. It cannot run anything."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CheckKind
    affects: tuple[HypothesisKind, ...] = Field(min_length=1)
    read_only: Literal[True] = True


class AgentOutput(BaseModel):
    """The structure a model's final answer must parse into.

    ``root_cause_assessment`` is a literal, so any other value, such as a
    confirmed cause, is a malformed answer rather than a claim to review.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: InvestigationId
    root_cause_assessment: Literal["not_assessed"]
    claims: tuple[AgentClaim, ...] = Field(max_length=MAX_CLAIMS)
    proposed_checks: tuple[ProposedCheck, ...] = Field(default=(), max_length=64)


class AnswerFormat(StrEnum):
    """The shape of the final answer a provider is asked to submit.

    ``full`` carries each claim with its reference and optional text. ``compact``
    carries only record identifiers, and trusted code rebuilds the rest.
    """

    FULL = "full"
    COMPACT = "compact"


class UncertaintyRef(BaseModel):
    """One missing-evidence entry, limitation, or the ranking caveat, by ID.

    Missing evidence and limitations have no ID of their own. Their record ID is
    the position the report stores them at, the same key the verifier uses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal[
        RecordSource.MISSING_EVIDENCE,
        RecordSource.LIMITATION,
        RecordSource.RANKING_CAVEAT,
    ]
    record_id: str = Field(min_length=1, max_length=MAX_RECORD_ID_CHARS)


class CompactAgentOutput(BaseModel):
    """A final answer that names records by ID, without text or event lists.

    Each list keeps its kind, so an observation, an unconfirmed hypothesis, and an
    unknown stay distinct. The server rebuilds each reference's events and each
    claim's text from the canonical report, so nothing the model writes is
    returned as evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: InvestigationId
    root_cause_assessment: Literal["not_assessed"]
    observations: tuple[ObservationId, ...] = Field(default=(), max_length=MAX_CLAIMS)
    hypotheses: tuple[HypothesisKind, ...] = Field(default=(), max_length=16)
    uncertainties: tuple[UncertaintyRef, ...] = Field(
        default=(),
        max_length=MAX_CLAIMS,
    )
    proposed_checks: tuple[ProposedCheck, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def _claims_within_limit(self) -> Self:
        """Bound the total number of references, as the full answer is bounded."""
        total = len(self.observations) + len(self.hypotheses) + len(self.uncertainties)
        if total > MAX_CLAIMS:
            raise ValueError("the answer names more records than it may carry")
        return self


class AgentStatus(StrEnum):
    """How an agent run ended. Only ``verified`` carries claims."""

    VERIFIED = "verified"
    REJECTED = "rejected"
    MALFORMED_OUTPUT = "malformed_output"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROVIDER_ERROR = "provider_error"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"


class VerificationStatus(StrEnum):
    """Whether the returned claims were checked against the report."""

    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"


class FailureCode(StrEnum):
    """Why the verifier rejected an answer. Each code has one fixed message."""

    CROSS_INCIDENT_REFERENCE = "cross_incident_reference"
    UNKNOWN_REFERENCE = "unknown_reference"
    KIND_MISMATCH = "kind_mismatch"
    TEXT_NOT_CANONICAL = "text_not_canonical"
    UNKNOWN_EVENT = "unknown_event"
    EVENT_NOT_CITED = "event_not_cited"
    UNSUPPORTED_CHECK = "unsupported_check"
    MISSING_CAVEAT = "missing_caveat"
    INSUFFICIENT_EVIDENCE_CLAIM = "insufficient_evidence_claim"
    REPORT_PROVENANCE = "report_provenance"


class VerificationFailure(BaseModel):
    """One reason an answer was rejected. It names a claim index, not its text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: FailureCode
    message: str = Field(min_length=1)
    claim_index: int | None = Field(default=None, ge=0)


class RequestDiagnostic(BaseModel):
    """Token and admission facts for one provider request. Holds no content.

    A refused attempt was never sent, so it has no provider counts, and it is
    listed only to show which limit stopped the run. ``request_number`` is the
    number the attempt would have had. The reserve is the estimated input plus
    the output allowance, the figure the budget compares with the tokens left.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_number: Annotated[int, Field(ge=1)]
    serialized_chars: Annotated[int, Field(ge=0)]
    estimated_input_tokens: Annotated[int, Field(ge=0)]
    reserved_output_tokens: Annotated[int, Field(ge=0)]
    remaining_tokens_before: int
    admitted: bool
    refusal: BudgetStop | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _refusal_only_when_not_admitted(self) -> Self:
        """Tie the admission flag to the refusal reason."""
        if self.admitted != (self.refusal is None):
            raise ValueError("a request is admitted exactly when it has no refusal")
        return self


class RunAccounting(BaseModel):
    """What one run used and how it ended.

    Token counts come from the provider when it reports them. Cost is an estimate
    from configured prices, and it is None, meaning unknown, when no price applies.
    It is never reported as zero for that reason. Latency is end to end, measured
    locally. ``request_diagnostics`` lists each attempt in order, bounded by the
    request limit plus one refused attempt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str | None = Field(default=None, max_length=100)
    provider_requests: Annotated[int, Field(ge=0)] = 0
    completed_tool_calls: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    total_tokens: Annotated[int, Field(ge=0)] = 0
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    cost_basis: str = Field(default="unknown: no price configured", max_length=400)
    latency_seconds: Annotated[float, Field(ge=0)] = 0.0
    final_status: AgentStatus
    budget_stop: BudgetStop | None = None
    request_diagnostics: tuple[RequestDiagnostic, ...] = Field(
        default=(),
        max_length=64,
    )


class AgentRun(BaseModel):
    """The result of one agent run over one investigation.

    A run that is not verified returns no claims and no proposed checks, so a
    caller cannot read content the verifier did not accept. ``token_usage`` sums
    every reply the provider reported, including refused and incomplete ones.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: InvestigationId
    provider_id: str = Field(min_length=1)
    status: AgentStatus
    verification_status: VerificationStatus
    tool_calls: Annotated[int, Field(ge=0)]
    claims: tuple[AgentClaim, ...] = ()
    proposed_checks: tuple[ProposedCheck, ...] = ()
    failures: tuple[VerificationFailure, ...] = ()
    token_usage: TokenUsage = TokenUsage()
    provider_failure: ProviderFailureReason | None = None
    # Safe diagnostics for a provider failure: the HTTP status and the provider's
    # request identifier. Both are optional, and neither carries provider text.
    provider_http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    provider_request_id: str | None = Field(
        default=None,
        pattern=PROVIDER_REQUEST_ID_PATTERN,
    )
    tool_successes: Annotated[int, Field(ge=0)] = 0
    tool_rejections: Annotated[int, Field(ge=0)] = 0
    budget_stop: BudgetStop | None = None
    accounting: RunAccounting | None = None

    @model_validator(mode="after")
    def _content_only_when_verified(self) -> Self:
        """Tie the status, verification, claims, and failures together."""
        verified = self.verification_status is VerificationStatus.VERIFIED
        if verified != (self.status is AgentStatus.VERIFIED):
            raise ValueError("status and verification status must agree")
        if verified and self.failures:
            raise ValueError("a verified run has no failures")
        if not verified and (self.claims or self.proposed_checks):
            raise ValueError("an unverified run carries no claims or checks")
        if verified and any(claim.text is None for claim in self.claims):
            raise ValueError("a verified claim carries its canonical text")
        return self

    @property
    def observations(self) -> tuple[AgentClaim, ...]:
        """Verified observations: facts computed from stored events."""
        return tuple(c for c in self.claims if c.kind is ClaimKind.OBSERVATION)

    @property
    def hypotheses(self) -> tuple[AgentClaim, ...]:
        """Existing root-cause candidates. Each is unconfirmed, never a finding."""
        return tuple(c for c in self.claims if c.kind is ClaimKind.HYPOTHESIS)

    @property
    def uncertainties(self) -> tuple[AgentClaim, ...]:
        """Missing evidence, limitations, and the ranking caveat."""
        return tuple(c for c in self.claims if c.kind is ClaimKind.UNCERTAINTY)
