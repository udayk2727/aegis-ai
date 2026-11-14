"""Simulated SDK replies for the provider tests and the tests that share them.

These doubles hold only what the adapters read from a reply. They import no SDK,
so tests that use them still run when the optional OpenAI or Anthropic SDK is not
installed.
"""

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any


class UnexpectedRequest(BaseException):
    """Raised when the adapter sends more requests than the test expected.

    It derives from BaseException, so the adapter's own handling cannot hide it.
    """


class FakeMessagesApi:
    """Replays replies in order. Each reply is a message object or an exception."""

    def __init__(self, replies: Sequence[Any]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        """Record the request and return or raise the next scripted reply."""
        self.requests.append(kwargs)
        if not self._replies:
            raise UnexpectedRequest("the test expected no further request")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _message(
    blocks: Sequence[Any],
    *,
    stop_reason: str | None = "tool_use",
    usage: tuple[int, int] | None = (10, 5),
) -> SimpleNamespace:
    """A simulated message with the fields the adapter reads."""
    usage_object = (
        None
        if usage is None
        else SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
    )
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=usage_object,
    )


def _tool_use(name: str, tool_input: Any, block_id: str = "toolu_1") -> SimpleNamespace:
    """A tool_use content block, as the SDK returns one."""
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


class FakeResponsesApi:
    """Replays replies in order. Each reply is a response object or an exception."""

    def __init__(self, replies: Sequence[Any]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        """Record the request and return or raise the next scripted reply."""
        self.requests.append(kwargs)
        if not self._replies:
            raise UnexpectedRequest("the test expected no further request")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _response(
    output: Sequence[Any],
    *,
    status: str = "completed",
    usage: tuple[int, int] | None = (10, 5),
) -> SimpleNamespace:
    """A simulated Responses object with the fields the adapter reads."""
    usage_object = (
        None
        if usage is None
        else SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
    )
    return SimpleNamespace(output=list(output), status=status, usage=usage_object)


def _call(name: str, arguments: Any, call_id: str = "call_1") -> SimpleNamespace:
    """A function-call output item. Dict arguments are encoded as the API does."""
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=text,
        call_id=call_id,
    )
