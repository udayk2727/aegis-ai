"""Minimal Anthropic Messages connectivity diagnostic. Local development only.

It sends one synthetic user message, "Reply with OK.", with no tools, no system text,
no thinking settings, and no investigation context. It does not test agent tool
calling, thinking-block replay, or evidence verification. Those need the agent CLI.

    python scripts/anthropic_connectivity_check.py          # offline checks only
    python scripts/anthropic_connectivity_check.py --live   # one paid request

Without ``--live`` the script reads the configuration, builds the request, and
constructs the SDK client with a local check. It sends nothing. With ``--live`` it
sends exactly one request, with zero SDK retries and a small output limit.

Configuration comes from the environment only: ``ANTHROPIC_API_KEY`` and
``AEGIS_LLM_MODEL``, which has no default. The script never prints the key, a
request body, a response header, or the SDK's error message. A failure shows the
HTTP status, a sanitized request ID, an error category, and an optional reason, both
from fixed lists. The reason is matched from the provider's message but never printed.

Exit codes: 0 means the offline checks passed, or the live request succeeded. 1 means
the live request failed. 2 means the configuration is missing or invalid.
"""

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from anthropic import (  # noqa: E402
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from app.config import (  # noqa: E402
    ANTHROPIC_API_KEY_VARIABLE,
    ConfigurationError,
    load_live_model_settings,
)
from app.services.agent_transport import safe_request_id  # noqa: E402

PROMPT = "Reply with OK."
MAX_TOKENS = 16
TIMEOUT_SECONDS = 30.0
ALLOWED_REQUEST_KEYS = frozenset({"model", "max_tokens", "messages"})

# Error types the API documents. Only these are echoed. Any other type is reported
# as the status-based category below, so a provider's free text never reaches output.
ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "billing_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
        "rate_limit_error",
        "api_error",
        "overloaded_error",
        "timeout_error",
    }
)
STATUS_CATEGORIES: dict[int, str] = {
    400: "invalid_request",
    401: "authentication",
    402: "billing",
    403: "permission",
    404: "not_found",
    429: "rate_limited",
}
# Fixed phrases matched, case-insensitively, against the provider's error message.
# The message itself is never printed; only the label of the first match is. A
# documented error type can hide the real cause, such as insufficient credits
# arriving as an invalid_request_error, so the phrase is checked as well. Wording
# is not a documented contract, so an unmatched message reports no reason.
REASON_PHRASES: tuple[tuple[str, str], ...] = (
    ("insufficient_credits", "credit balance is too low"),
    ("spending_limit", "usage limit"),
    ("spending_limit", "spend limit"),
    ("invalid_model", "model:"),
    ("unsupported_feature", "not supported"),
)

MakeClient = Callable[[str], Any]


def build_request(model: str) -> dict[str, Any]:
    """The one request this diagnostic sends. Nothing else is added to it."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": PROMPT}],
    }


def check_request(request: Mapping[str, Any]) -> None:
    """Refuse any request that is not the minimal diagnostic shape."""
    if set(request) != ALLOWED_REQUEST_KEYS:
        raise ValueError("request carries fields the diagnostic does not allow")
    if not 1 <= request["max_tokens"] <= MAX_TOKENS:
        raise ValueError("max_tokens is outside the diagnostic limit")
    messages = request["messages"]
    if messages != [{"role": "user", "content": PROMPT}]:
        raise ValueError("messages are not the synthetic prompt")


def build_client(api_key: str) -> Anthropic:
    """Create the SDK client with SDK retries off. Constructing it sends nothing."""
    return Anthropic(api_key=api_key, max_retries=0, timeout=TIMEOUT_SECONDS)


def _status_category(error: APIStatusError) -> str:
    """Pick the category: a documented error type if one was sent, else the status."""
    body = error.body
    if isinstance(body, dict):
        detail = body.get("error")
        if isinstance(detail, dict) and detail.get("type") in ERROR_TYPES:
            return str(detail["type"])
    if error.status_code in STATUS_CATEGORIES:
        return STATUS_CATEGORIES[error.status_code]
    return "server_error" if error.status_code >= 500 else "unexpected_status"


def _failure_reason(error: APIStatusError) -> str | None:
    """Return the label of the first known phrase in the provider's message, if any."""
    body = error.body
    if not isinstance(body, dict):
        return None
    detail = body.get("error")
    if not isinstance(detail, dict) or not isinstance(detail.get("message"), str):
        return None
    message = detail["message"].lower()
    for reason, phrase in REASON_PHRASES:
        if phrase in message:
            return reason
    return None


def _send(client: Any, request: Mapping[str, Any]) -> int:
    """Send the one request and report only safe fields. Returns the exit code."""
    try:
        message = client.messages.create(**request)
    except APITimeoutError:
        print("result: failed")
        print("category: timeout")
        return 1
    except APIConnectionError:
        print("result: failed")
        print("category: network")
        return 1
    except APIStatusError as error:
        print("result: failed")
        print(f"http_status: {error.status_code}")
        print(f"category: {_status_category(error)}")
        print(f"reason: {_failure_reason(error) or 'not classified'}")
        request_id = safe_request_id(error.request_id)
        print(f"request_id: {request_id or 'not provided'}")
        return 1
    usage = getattr(message, "usage", None)
    print("result: ok")
    print(f"stop_reason: {getattr(message, 'stop_reason', None)}")
    print(f"input_tokens: {getattr(usage, 'input_tokens', 'unknown')}")
    print(f"output_tokens: {getattr(usage, 'output_tokens', 'unknown')}")
    request_id = safe_request_id(getattr(message, "_request_id", None))
    print(f"request_id: {request_id or 'not provided'}")
    return 0


def main(
    argv: Sequence[str],
    environ: Mapping[str, str],
    make_client: MakeClient = build_client,
) -> int:
    """Run the offline checks, and the one live request only with ``--live``."""
    live = "--live" in argv
    try:
        settings = load_live_model_settings(
            environ,
            key_variable=ANTHROPIC_API_KEY_VARIABLE,
        )
    except ConfigurationError as error:
        print(f"configuration: {error}")
        return 2
    request = build_request(settings.model)
    check_request(request)
    client = make_client(settings.api_key.get_secret_value())
    if client.max_retries != 0:
        print("configuration: SDK retries must be off")
        return 2
    print(f"model: {settings.model}")
    print(f"max_tokens: {MAX_TOKENS}")
    print("offline checks: passed")
    if not live:
        print("live request: not sent (pass --live to send one request)")
        return 0
    return _send(client, request)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
