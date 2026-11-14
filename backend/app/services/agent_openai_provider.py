"""OpenAI Responses API adapter for the investigation agent. Local development only.

This is the only module that imports the OpenAI SDK. The CLI imports it when it is
asked for ``--provider openai``, and nothing else does. Importing it makes no
request. Only ``next_turn`` on an adapter built with a live client can do that.

Design:

- Every request is stateless. It carries the whole transcript, with ``store``
  set to false, so OpenAI keeps no response and the adapter keeps no conversation
  state. The one exception is a queue for a reply that holds several tool calls.
- Tools are the trusted registry in ``agent_tools``, declared as functions with
  their argument models as schemas. The model can request them, but only
  ``dispatch`` runs them.
- The final answer is a call to ``submit_answer``, whose parameters are the
  ``AgentOutput`` schema, or the ``CompactAgentOutput`` schema when the run asks for
  the compact format. Pydantic parses it, and the verifier checks it, as it does for
  every answer.
- Every SDK error becomes a fixed ``ProviderFailureReason``. The SDK's own message
  is dropped, because it can echo request details. The SDK's built-in retries are
  turned off, so the bounded retry loop here is the only one.
- Tool outputs and telemetry reach the model only as data inside
  ``function_call_output`` items. They are never placed in ``instructions``.
"""

import json
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
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
from app.services.agent_transport import FailureClass, send_within_budget
from app.services.agent_tools import TOOLS

PROVIDER_ID_PREFIX = "openai-responses"
SUBMIT_TOOL = "submit_answer"
MAX_CALLS_PER_REPLY = 8

SUBMIT_NOTE = (
    f"Give the final answer by calling the {SUBMIT_TOOL} function once, with the "
    "AgentOutput arguments. Do not answer in plain text."
)
COMPACT_SUBMIT_NOTE = (
    f"Give the final answer by calling the {SUBMIT_TOOL} function once, with the "
    "CompactAgentOutput arguments: record IDs only, and no text. Do not answer in "
    "plain text."
)


class ResponsesApi(Protocol):
    """The one SDK method the adapter uses. Tests pass a fake that has it."""

    def create(self, **kwargs: Any) -> Any:
        """Create one response, as the SDK's Responses resource does."""
        ...


def tool_definitions(
    answer_format: AnswerFormat = AnswerFormat.FULL,
) -> list[dict[str, Any]]:
    """Declare each allowlisted tool, and the answer function, as Responses tools.

    Schemas come from the same Pydantic models that validate each call, so the
    model is shown exactly the arguments that will be accepted. The answer
    function's schema is the one for ``answer_format``.
    """
    definitions: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.arguments.model_json_schema(),
            "strict": False,
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
            "type": "function",
            "name": SUBMIT_TOOL,
            "description": answer_description,
            "parameters": answer_schema,
            "strict": False,
        }
    )
    return definitions


class OpenAIResponsesProvider:
    """Provider that asks the OpenAI Responses API for each turn."""

    def __init__(
        self,
        api: ResponsesApi,
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
        """Translate the transcript into one stateless Responses request."""
        instructions = [m.content for m in transcript if m.role is MessageRole.SYSTEM]
        items: list[dict[str, Any]] = []
        for message in transcript:
            if message.role is MessageRole.USER:
                items.append({"role": "user", "content": message.content})
            elif message.role is MessageRole.ASSISTANT:
                items.append(_function_call_item(message))
            elif message.role is MessageRole.TOOL:
                if message.call_id is None:
                    raise ProviderError(ProviderFailureReason.MALFORMED)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.call_id,
                        "output": message.content,
                    }
                )
        note = (
            COMPACT_SUBMIT_NOTE
            if self._answer_format is AnswerFormat.COMPACT
            else SUBMIT_NOTE
        )
        return {
            "model": self._model,
            "instructions": "\n\n".join([*instructions, note]),
            "input": items,
            "tools": tool_definitions(self._answer_format),
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "max_output_tokens": self._budget.limits.max_output_tokens,
        }

    def _send(self, request: dict[str, Any]) -> Any:
        """Send one request under the run's budget. See ``agent_transport``."""
        return send_within_budget(
            request,
            self._budget,
            send=lambda body, timeout: self._api.create(**body, timeout=timeout),
            classify=_classify_openai_error,
            sleep=self._sleep,
        )

    def _parse(self, response: Any) -> tuple[ProviderTurn, ...]:
        """Turn a response into turns. The first turn carries the response's usage."""
        usage = _usage_of(response)
        if getattr(response, "status", None) == "incomplete":
            raise ProviderError(ProviderFailureReason.INCOMPLETE, usage=usage)
        output = getattr(response, "output", None)
        if not isinstance(output, list):
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)

        calls: list[Any] = []
        texts: list[str] = []
        for item in output:
            kind = getattr(item, "type", None)
            if kind == "function_call":
                calls.append(item)
            elif kind == "message":
                for part in getattr(item, "content", None) or []:
                    part_type = getattr(part, "type", None)
                    if part_type == "refusal":
                        raise ProviderError(ProviderFailureReason.REFUSED, usage=usage)
                    if part_type == "output_text":
                        texts.append(str(getattr(part, "text", "")))

        if len(calls) > MAX_CALLS_PER_REPLY:
            raise ProviderError(ProviderFailureReason.MALFORMED, usage=usage)
        try:
            if calls:
                turns = [_tool_turn(call) for call in calls]
            elif texts:
                turns = [ProviderTurn(final_text="".join(texts))]
            else:
                raise ProviderError(ProviderFailureReason.MALFORMED)
        except ProviderError as error:
            raise ProviderError(error.reason, usage=usage) from None
        return (turns[0].model_copy(update={"usage": usage}), *turns[1:])


def _classify_openai_error(error: Exception) -> FailureClass:
    """Map an SDK error to a fixed reason, and whether it is transient.

    The order matters, because the timeout and rate-limit errors are subclasses of
    the connection and status errors. Anything unrecognised is a non-retryable API
    error, and the SDK's message is never returned.
    """
    if isinstance(error, APITimeoutError):
        return FailureClass(ProviderFailureReason.TIMEOUT, retryable=True)
    if isinstance(error, RateLimitError):
        return FailureClass(ProviderFailureReason.RATE_LIMITED, retryable=True)
    if isinstance(error, APIConnectionError):
        return FailureClass(ProviderFailureReason.NETWORK, retryable=True)
    if isinstance(error, APIStatusError):
        return FailureClass(
            ProviderFailureReason.API_ERROR,
            retryable=error.status_code >= 500,
        )
    return FailureClass(ProviderFailureReason.API_ERROR, retryable=False)


def _function_call_item(message: ProviderMessage) -> dict[str, Any]:
    """Replay one earlier tool request exactly as the model made it."""
    try:
        call = ToolCall.model_validate_json(message.content)
    except ValidationError:
        raise ProviderError(ProviderFailureReason.MALFORMED) from None
    if call.call_id is None:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    return {
        "type": "function_call",
        "call_id": call.call_id,
        "name": call.name,
        "arguments": json.dumps(call.arguments, sort_keys=True),
    }


def _tool_turn(call: Any) -> ProviderTurn:
    """Translate one function-call item into a turn, or refuse it as malformed."""
    name = getattr(call, "name", None)
    call_id = getattr(call, "call_id", None)
    arguments = getattr(call, "arguments", None)
    fields = (name, call_id, arguments)
    if not all(isinstance(value, str) and value for value in fields):
        raise ProviderError(ProviderFailureReason.MALFORMED)
    if len(arguments) > MAX_MESSAGE_CHARS:
        raise ProviderError(ProviderFailureReason.MALFORMED)
    if name == SUBMIT_TOOL:
        return ProviderTurn(final_text=arguments)
    try:
        parsed = json.loads(arguments)
        return ProviderTurn(
            tool_call=ToolCall(name=name, arguments=parsed, call_id=call_id),
        )
    except (ValueError, ValidationError):
        raise ProviderError(ProviderFailureReason.MALFORMED) from None


def _usage_of(response: Any) -> TokenUsage | None:
    """Read the token counts a response reports, or None if they are absent."""
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


def build_live_provider(
    settings: LiveModelSettings,
    budget: RunBudget | None = None,
    answer_format: AnswerFormat = AnswerFormat.FULL,
) -> OpenAIResponsesProvider:
    """Create the live provider for one run. Constructing the client makes no request.

    The SDK's built-in retries are off, so the adapter's own budgeted loop is the
    only retry path. Each request also gets a timeout, set per request from the
    run's remaining deadline. The client-level timeout is only a fallback.
    """
    run_budget = budget if budget is not None else RunBudget(BudgetLimits())
    client = OpenAI(
        api_key=settings.api_key.get_secret_value(),
        timeout=run_budget.limits.request_timeout_seconds,
        max_retries=0,
    )
    return OpenAIResponsesProvider(
        client.responses,
        model=settings.model,
        budget=run_budget,
        answer_format=answer_format,
    )
