"""Anthropic Messages API adapter for the investigation agent. Local development only.

This is the only module that imports the Anthropic SDK. The CLI and the evaluation
import it only with ``--provider anthropic --live``. Importing it makes no request.

Design:

- Every request is stateless. It carries the whole conversation: the user's
  investigation ID, each tool request as a ``tool_use`` block, and each result as a
  ``tool_result`` block that names the same ``tool_use_id``. The adapter keeps no
  conversation state, apart from a queue for a reply that holds several tool
  requests.
- Tools are the trusted registry in ``agent_tools``, declared client-side with
  ``input_schema`` taken from the same Pydantic models that validate each call. The
  SDK never runs a function. Only ``dispatch`` runs a tool, and the orchestrator
  calls it.
- The final answer is a call to ``submit_answer``, whose input schema is the
  ``AgentOutput`` schema, or the ``CompactAgentOutput`` schema when the run asks for
  the compact format. Pydantic validates it, and the verifier checks it, exactly as
  for every other provider. Plain text is also accepted as a final answer, and the
  verifier applies to it too.
- Parallel tool use is disabled, so each reply normally holds one tool request.
- A reply with one tool request keeps its content blocks in order, with thinking
  blocks and signatures, and the same blocks are replayed before the tool result.
  The API rejects a changed thinking block, so nothing is reordered or edited. A
  reply with several tool requests and any thinking block is malformed.
- The budgeted transport in ``agent_transport`` sends every request. It is shared
  with the OpenAI adapter, so the budget rules are the same for both.
- Every SDK error becomes a fixed ``ProviderFailureReason``. The SDK's message is
  dropped, because it can echo request details. The SDK's own retries are off.
- Tool outputs and telemetry reach the model only as ``tool_result`` content. They
  never enter the ``system`` text, which holds only the trusted instructions.

Retention: this adapter sends no server-side conversation state, but the provider's
own retention and data policies still apply to any request that is sent. Check
them before any live use.
"""

import json
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)
from pydantic import ValidationError

from app.config import LiveModelSettings
from app.domain.agent_records import (
    MAX_MESSAGE_CHARS,
    AgentOutput,
    AnswerFormat,
    BudgetLimits,
    CompactAgentOutput,
    MessageRole,
    ProviderFailureReason,
    ProviderMessage,
    ProviderTurn,
    TokenUsage,
    ToolCall,
)
from app.services.agent_budget import RunBudget
from app.services.agent_provider import ProviderError
from app.services.agent_tools import TOOLS
from app.services.agent_transport import (
    FailureClass,
    safe_request_id,
    send_within_budget,
)

PROVIDER_ID_PREFIX = "anthropic-messages"
SUBMIT_TOOL = "submit_answer"
MAX_CALLS_PER_REPLY = 8
ACCEPTED_STOP_REASONS = frozenset({"end_turn", "tool_use", "stop_sequence"})
UNSUPPORTED_WIRE_KEYWORDS = frozenset({"minLength", "maxLength", "maxItems"})
# Titles are display labels that Pydantic generates for every field and model. They
# never change what a value validates against, so the wire schema omits them.
PRESENTATION_WIRE_KEYWORDS = frozenset({"title"})
DATA_KEYWORDS = frozenset({"enum", "const", "default"})
# The fields the API needs to replay each block type exactly. A thinking block's
# signature must come back unchanged, or the API rejects the request.
REPLAY_FIELDS: dict[str, tuple[str, ...]] = {
    "thinking": ("thinking", "signature"),
    "redacted_thinking": ("data",),
    "text": ("text",),
    "tool_use": ("id", "name", "input"),
}
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

SUBMIT_NOTE = (
    f"Give the final answer by calling the {SUBMIT_TOOL} tool once, with the "
    "AgentOutput arguments. Do not answer in plain text."
)
COMPACT_SUBMIT_NOTE = (
    f"Give the final answer by calling the {SUBMIT_TOOL} tool once, with the "
    "CompactAgentOutput arguments: record IDs only, and no text. Do not answer in "
    "plain text."
)


class MessagesApi(Protocol):
    """The one SDK method the adapter uses. Tests pass a fake that has it."""

    def create(self, **kwargs: Any) -> Any:
        """Create one message, as the SDK's Messages resource does."""
        ...


def tool_definitions(
    answer_format: AnswerFormat = AnswerFormat.FULL,
) -> list[dict[str, Any]]:
    """Declare each allowlisted tool, and the answer tool, as client-side tools.

    The answer tool's schema is the one for ``answer_format``, so the model is
    shown exactly the answer that will be parsed.
    """
    definitions: list[dict[str, Any]] = [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": wire_schema(spec.arguments.model_json_schema()),
        }
        for spec in TOOLS
    ]
    if answer_format is AnswerFormat.COMPACT:
        answer_schema: dict[str, Any] = CompactAgentOutput.model_json_schema()
        answer_description = (
            "Submit the compact final answer: record IDs only, for this investigation."
        )
    else:
        answer_schema = AgentOutput.model_json_schema()
        answer_description = "Submit the final AgentOutput for this investigation."
    definitions.append(
        {
            "name": SUBMIT_TOOL,
            "description": answer_description,
            "input_schema": wire_schema(answer_schema),
        }
    )
    return definitions


def wire_schema(schema: Any) -> Any:
    """Drop keywords the API does not need or does not accept from the schema.

    The API documents ``minLength``, ``maxLength``, and ``maxItems`` as unsupported,
    and an unsupported keyword is a 400. The removed limits are still enforced: the
    adapter validates every answer with ``AgentOutput`` and the verifier checks it
    again. ``title`` is dropped because it is display metadata, and descriptions are
    kept because they guide the model. Keys under ``properties`` and ``$defs`` are
    field names, not keywords, so they are kept, even when a field is called
    ``title``. Values under ``enum``, ``const``, and ``default`` are data, so they
    are left as they are.
    """
    if isinstance(schema, list):
        return [wire_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in UNSUPPORTED_WIRE_KEYWORDS or key in PRESENTATION_WIRE_KEYWORDS:
            continue
        if key in {"properties", "$defs"} and isinstance(value, dict):
            cleaned[key] = {name: wire_schema(sub) for name, sub in value.items()}
        elif key in DATA_KEYWORDS:
            cleaned[key] = value
        else:
            cleaned[key] = wire_schema(value)
    return cleaned


class AnthropicMessagesProvider:
    """Provider that asks the Anthropic Messages API for each turn."""

    def __init__(
        self,
        api: MessagesApi,
        *,
        model: str,
        budget: RunBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
        answer_format: AnswerFormat = AnswerFormat.FULL,
    ) -> None:
        if not model:
            raise ValueError("a model identifier is required")
        self.provider_id = f"{PROVIDER_ID_PREFIX}:{model}"
        self.model_id = model
        self._api = api
        self._model = model
        self._budget = budget if budget is not None else RunBudget(BudgetLimits())
        self._sleep = sleep
        self._answer_format = answer_format
        self._queued: deque[ProviderTurn] = deque()

    def next_turn(self, transcript: Sequence[ProviderMessage]) -> ProviderTurn:
        """Return the next turn, from the queue if one is waiting, else from the API."""
        if self._queued:
            return self._queued.popleft()
        response = self._send(self._request(transcript))
        turns = self._parse(response)
        self._queued.extend(turns[1:])
        return turns[0]

    def _request(self, transcript: Sequence[ProviderMessage]) -> dict[str, Any]:
        """Translate the transcript into one stateless Messages request."""
        system = [m.content for m in transcript if m.role is MessageRole.SYSTEM]
        messages: list[dict[str, Any]] = []
        for message in transcript:
            if message.role is MessageRole.USER:
                messages.append({"role": "user", "content": message.content})
            elif message.role is MessageRole.ASSISTANT:
                messages.append(_tool_use_message(message))
            elif message.role is MessageRole.TOOL:
                messages.append(_tool_result_message(message))
        note = (
            COMPACT_SUBMIT_NOTE
            if self._answer_format is AnswerFormat.COMPACT
            else SUBMIT_NOTE
        )
        return {
            "model": self._model,
            "max_tokens": self._budget.limits.max_output_tokens,
            "system": "\n\n".join([*system, note]),
            "messages": messages,
            "tools": tool_definitions(self._answer_format),
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }

    def _send(self, request: dict[str, Any]) -> Any:
        """Send one request under the run's budget. See ``agent_transport``."""
        return send_within_budget(
            request,
            self._budget,
            send=lambda body, timeout: self._api.create(**body, timeout=timeout),
            classify=_classify_anthropic_error,
            sleep=self._sleep,
        )

    def _parse(self, response: Any) -> tuple[ProviderTurn, ...]:
        """Turn a message into turns. The first turn carries the message's usage."""
        usage = _usage_of(response)
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise ProviderError(ProviderFailureReason.REFUSED, usage=usage)
        if stop_reason in {"max_tokens", "pause_turn"}:
            raise ProviderError(ProviderFailureReason.INCOMPLETE, usage=usage)
        if stop_reason not in ACCEPTED_STOP_REASONS:
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)

        content = getattr(response, "content", None)
        if not isinstance(content, list):
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)
        calls = [
            block for block in content if getattr(block, "type", None) == "tool_use"
        ]
        texts = [
            str(getattr(block, "text", ""))
            for block in content
            if getattr(block, "type", None) == "text"
        ]
        if len(calls) > MAX_CALLS_PER_REPLY:
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)
        replay: tuple[dict[str, Any], ...] = ()
        if len(calls) == 1:
            replay = _replay_blocks(content, usage)
        elif any(getattr(b, "type", None) in THINKING_TYPES for b in content):
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)

        try:
            if calls:
                turns = [_tool_turn(block, replay) for block in calls]
            elif texts:
                turns = [ProviderTurn(final_text="".join(texts))]
            else:
                raise ProviderError(ProviderFailureReason.MALFORMED)
        except ProviderError as error:
            raise ProviderError(error.reason, usage=usage) from None
        return (turns[0].model_copy(update={"usage": usage}), *turns[1:])


def _tool_use_message(message: ProviderMessage) -> dict[str, Any]:
    """Replay one earlier tool request exactly as the model made it.

    When the reply carried blocks the API requires back, such as thinking blocks,
    the whole assistant content is replayed in its original order instead.
    """
    try:
        call = ToolCall.model_validate_json(message.content)
    except ValidationError:
        raise ProviderError(ProviderFailureReason.MALFORMED) from None
    if call.call_id is None:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    if message.replay_blocks:
        return {"role": "assistant", "content": _checked_replay(message, call)}
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            }
        ],
    }


def _replay_blocks(
    content: list[Any],
    usage: TokenUsage | None,
) -> tuple[dict[str, Any], ...]:
    """Copy one reply's content blocks, in order, so they can be replayed exactly.

    Only the fields the API accepts are copied, so SDK-only fields never reach a
    request. A block of any other type is refused, because it could not be replayed
    exactly.
    """
    copied: list[dict[str, Any]] = []
    for block in content:
        kind = getattr(block, "type", None)
        fields = REPLAY_FIELDS.get(kind)
        if fields is None:
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)
        item: dict[str, Any] = {"type": kind}
        for field in fields:
            value = getattr(block, field, None)
            if not isinstance(value, str | dict):
                raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)
            item[field] = value
        copied.append(item)
    return tuple(copied)


def _checked_replay(message: ProviderMessage, call: ToolCall) -> list[dict[str, Any]]:
    """Return the stored blocks only if they still match the recorded tool request."""
    tool_uses = [b for b in message.replay_blocks if b.get("type") == "tool_use"]
    if len(tool_uses) != 1:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    tool_use = tool_uses[0]
    if (
        tool_use.get("id") != call.call_id
        or tool_use.get("name") != call.name
        or tool_use.get("input") != call.arguments
    ):
        raise ProviderError(ProviderFailureReason.MALFORMED)
    return [dict(block) for block in message.replay_blocks]


def _tool_result_message(message: ProviderMessage) -> dict[str, Any]:
    """Answer one tool request. A refused request is sent back with is_error set."""
    if message.call_id is None:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    try:
        outcome = json.loads(message.content)
    except ValueError:
        raise ProviderError(ProviderFailureReason.MALFORMED) from None
    if not isinstance(outcome, dict):
        raise ProviderError(ProviderFailureReason.MALFORMED)
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": message.call_id,
                "content": message.content,
                "is_error": not bool(outcome.get("ok")),
            }
        ],
    }


def _tool_turn(block: Any, replay: tuple[dict[str, Any], ...] = ()) -> ProviderTurn:
    """Translate one tool_use block into a turn, or refuse it as malformed.

    The answer tool becomes a final answer. Any other name becomes a tool request,
    which the orchestrator then checks against the allowlist. An unknown name is
    refused there, and it is never run here.
    """
    name = getattr(block, "name", None)
    call_id = getattr(block, "id", None)
    arguments = getattr(block, "input", None)
    if not isinstance(name, str) or not name:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    if not isinstance(call_id, str) or not call_id:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    if not isinstance(arguments, dict):
        raise ProviderError(ProviderFailureReason.MALFORMED)
    encoded = json.dumps(arguments, sort_keys=True)
    if len(encoded) > MAX_MESSAGE_CHARS:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    if name == SUBMIT_TOOL:
        return ProviderTurn(final_text=encoded)
    try:
        return ProviderTurn(
            tool_call=ToolCall(name=name, arguments=arguments, call_id=call_id),
            replay_blocks=replay,
        )
    except ValidationError:
        raise ProviderError(ProviderFailureReason.MALFORMED) from None


def _usage_of(response: Any) -> TokenUsage | None:
    """Read the token counts a message reports, or None if they are absent."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
    except (TypeError, ValueError, ValidationError):
        return None


_CLIENT_ERROR_REASONS: dict[int, ProviderFailureReason] = {
    400: ProviderFailureReason.INVALID_REQUEST,
    401: ProviderFailureReason.AUTHENTICATION,
    403: ProviderFailureReason.PERMISSION,
    404: ProviderFailureReason.NOT_FOUND,
    422: ProviderFailureReason.INVALID_REQUEST,
    429: ProviderFailureReason.RATE_LIMITED,
}


def _classify_anthropic_error(error: Exception) -> FailureClass:
    """Map an SDK error to a fixed reason, with the safe diagnostics it carries.

    The order matters, because the timeout error is a subclass of the connection
    error. From a status error, only the status code and the request ID are kept.
    The SDK's message and body are never returned. Anything unrecognised is a
    non-retryable API error.
    """
    if isinstance(error, APITimeoutError):
        return FailureClass(ProviderFailureReason.TIMEOUT, retryable=True)
    if isinstance(error, APIConnectionError):
        return FailureClass(ProviderFailureReason.NETWORK, retryable=True)
    if isinstance(error, APIStatusError):
        status = error.status_code
        reason = _CLIENT_ERROR_REASONS.get(status)
        if reason is None:
            reason = (
                ProviderFailureReason.SERVER_ERROR
                if status >= 500
                else ProviderFailureReason.API_ERROR
            )
        return FailureClass(
            reason,
            retryable=status == 429 or status >= 500,
            http_status=status,
            request_id=safe_request_id(error.request_id),
        )
    return FailureClass(ProviderFailureReason.API_ERROR, retryable=False)


def build_live_provider(
    settings: LiveModelSettings,
    budget: RunBudget | None = None,
    answer_format: AnswerFormat = AnswerFormat.FULL,
) -> AnthropicMessagesProvider:
    """Create the live provider for one run. Constructing the client makes no request.

    The SDK's own retries are off, so the budgeted transport is the only retry path.
    The client-level timeout is only a fallback, because each request gets its own
    timeout from the run's remaining deadline.
    """
    run_budget = budget if budget is not None else RunBudget(BudgetLimits())
    client = Anthropic(
        api_key=settings.api_key.get_secret_value(),
        timeout=run_budget.limits.request_timeout_seconds,
        max_retries=0,
    )
    return AnthropicMessagesProvider(
        client.messages,
        model=settings.model,
        budget=run_budget,
        answer_format=answer_format,
    )
