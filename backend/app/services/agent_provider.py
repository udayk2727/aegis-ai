"""The model-provider interface the investigation agent calls.

A provider takes the transcript so far and returns one turn. The agent depends
only on this interface, so a real model adapter can replace the fake one without
changing the loop, the tools, or the verifier. The fake provider makes no network
call. The OpenAI adapter lives in ``agent_openai_provider`` and is imported only
when the local CLI asks for it.
"""

from collections.abc import Sequence
from typing import Protocol

from app.domain.agent_records import (
    ProviderFailureReason,
    ProviderMessage,
    ProviderTurn,
    TokenUsage,
)


class ProviderError(RuntimeError):
    """A provider could not produce a usable turn.

    The error carries a fixed reason and, when the provider reported one, the
    tokens spent before the failure. It may also carry the HTTP status and the
    provider's request ID, which are safe to show. It never carries provider text,
    so a provider's error message cannot leak into a caller's result.
    """

    def __init__(
        self,
        reason: ProviderFailureReason,
        *,
        usage: TokenUsage | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.usage = usage
        self.http_status = http_status
        self.request_id = request_id


class ModelProvider(Protocol):
    """Produces the next turn from the transcript.

    A provider raises ``ProviderError`` only when it cannot produce a usable turn.
    A bad answer is not an error here. The verifier refuses it.
    """

    provider_id: str

    def next_turn(self, transcript: Sequence[ProviderMessage]) -> ProviderTurn:
        """Return the model's next reply, given everything said so far."""
        ...
