"""Offline tests for the Anthropic connectivity diagnostic, with fake clients only.

Nothing here makes a network request or needs a credential. The fake client replays
one scripted reply, and each failure is an SDK exception built from a local request.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("anthropic", reason="the optional Anthropic SDK is not installed")

from anthropic import APIConnectionError, APIStatusError, APITimeoutError  # noqa: E402

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "anthropic_connectivity_check.py"
)
REQUEST = httpx.Request("POST", "https://api.invalid/v1/messages")
SENTINEL = "SENTINEL-NOT-A-KEY-diagnostic-4c2e"
KEY = "test-key-" + SENTINEL
MODEL = "claude-sonnet-5-5"
ENVIRON = {"ANTHROPIC_API_KEY": KEY, "AEGIS_LLM_MODEL": MODEL}


def _load() -> ModuleType:
    """Import the script by path, as the CLI tests do, without running main."""
    spec = importlib.util.spec_from_file_location(
        "anthropic_connectivity_check",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load()


class FakeMessages:
    """Records each request and returns or raises one scripted reply."""

    def __init__(self, reply: Any) -> None:
        self._reply = reply
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        """Record the request, then return or raise the scripted reply."""
        self.requests.append(kwargs)
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply


class FakeClient:
    """Stands in for the SDK client. It has the attribute the script checks."""

    max_retries = 0

    def __init__(self, reply: Any) -> None:
        self.messages = FakeMessages(reply)


def _run(
    reply: Any,
    argv: Sequence[str],
    environ: dict[str, str] | None = None,
) -> tuple[int, FakeClient | None]:
    """Run main with a fake client factory, and report whether one was built."""
    built: list[FakeClient] = []

    def make_client(key: str) -> FakeClient:
        assert key == KEY
        client = FakeClient(reply)
        built.append(client)
        return client

    code = CHECK.main(argv, ENVIRON if environ is None else environ, make_client)
    return code, (built[0] if built else None)


def _status_error(
    status: int,
    error_type: str | None = None,
    message: str = SENTINEL,
) -> APIStatusError:
    """An SDK status error with a request-id header and a scripted body."""
    body = {"error": {"type": error_type or "error", "message": message}}
    return APIStatusError(
        f"status {status} {SENTINEL}",
        response=httpx.Response(
            status,
            request=REQUEST,
            headers={"request-id": "req_011CDiagnostic"},
        ),
        body=body,
    )


def _ok_reply() -> SimpleNamespace:
    """A successful message with the fields the script reads."""
    return SimpleNamespace(
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=9, output_tokens=2),
        _request_id="req_011CDiagnostic",
    )


def test_the_request_is_the_minimal_synthetic_prompt() -> None:
    request = CHECK.build_request(MODEL)

    assert request == {
        "model": MODEL,
        "max_tokens": CHECK.MAX_TOKENS,
        "messages": [{"role": "user", "content": "Reply with OK."}],
    }
    CHECK.check_request(request)


def test_a_request_with_tools_or_extra_fields_is_refused() -> None:
    request = CHECK.build_request(MODEL)

    with pytest.raises(ValueError):
        CHECK.check_request({**request, "tools": []})
    with pytest.raises(ValueError):
        CHECK.check_request({**request, "max_tokens": CHECK.MAX_TOKENS + 1})


def test_the_default_client_disables_sdk_retries() -> None:
    client = CHECK.build_client(KEY)

    assert client.max_retries == 0


def test_without_live_the_offline_checks_pass_and_nothing_is_sent() -> None:
    code, client = _run(_ok_reply(), [])

    assert code == 0
    assert client is not None
    assert client.messages.requests == []


def test_offline_output_names_the_model_and_no_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(_ok_reply(), [])

    out = capsys.readouterr().out
    assert f"model: {MODEL}" in out
    assert "live request: not sent" in out
    assert SENTINEL not in out


def test_live_success_sends_exactly_one_request_and_reports_safe_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, client = _run(_ok_reply(), ["--live"])

    assert code == 0
    assert client is not None
    assert len(client.messages.requests) == 1
    assert client.messages.requests[0] == CHECK.build_request(MODEL)
    out = capsys.readouterr().out
    assert "result: ok" in out
    assert "input_tokens: 9" in out
    assert "output_tokens: 2" in out
    assert "request_id: req_011CDiagnostic" in out
    assert KEY not in out


@pytest.mark.parametrize(
    ("status", "error_type", "category"),
    [
        (400, "invalid_request_error", "invalid_request_error"),
        (401, "authentication_error", "authentication_error"),
        (403, "permission_error", "permission_error"),
        (404, "not_found_error", "not_found_error"),
        (429, "rate_limit_error", "rate_limit_error"),
    ],
)
def test_a_status_failure_reports_the_status_and_an_allowed_category(
    status: int,
    error_type: str,
    category: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, client = _run(_status_error(status, error_type), ["--live"])

    assert code == 1
    assert client is not None
    assert len(client.messages.requests) == 1
    out = capsys.readouterr().out
    assert f"http_status: {status}" in out
    assert f"category: {category}" in out
    assert "request_id: req_011CDiagnostic" in out
    assert SENTINEL not in out
    assert KEY not in out


@pytest.mark.parametrize(
    ("status", "category"),
    [(400, "invalid_request"), (401, "authentication"), (403, "permission")],
)
def test_a_status_without_a_documented_error_type_uses_the_status_category(
    status: int,
    category: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run(_status_error(status, error_type="unexpected text"), ["--live"])

    assert code == 1
    out = capsys.readouterr().out
    assert f"category: {category}" in out
    assert "unexpected text" not in out


def test_a_404_without_a_documented_type_reports_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run(_status_error(404), ["--live"])

    assert code == 1
    assert "category: not_found" in capsys.readouterr().out


def test_a_timeout_is_reported_without_a_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, client = _run(APITimeoutError(request=REQUEST), ["--live"])

    assert code == 1
    assert client is not None
    assert len(client.messages.requests) == 1
    out = capsys.readouterr().out
    assert "category: timeout" in out
    assert "http_status" not in out


def test_a_connection_failure_is_reported_without_a_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run(APIConnectionError(request=REQUEST), ["--live"])

    assert code == 1
    out = capsys.readouterr().out
    assert "category: network" in out
    assert "http_status" not in out


CREDIT_MESSAGE = "Your credit balance is too low to access the Anthropic API."
SPEND_MESSAGE = "You have reached your specified API usage limits."
MODEL_MESSAGE = "model: claude-not-a-real-model"
FEATURE_MESSAGE = "This parameter is not supported for the selected model."


@pytest.mark.parametrize(
    ("status", "error_type", "message", "reason"),
    [
        (400, "invalid_request_error", CREDIT_MESSAGE, "insufficient_credits"),
        (429, "rate_limit_error", SPEND_MESSAGE, "spending_limit"),
        (400, "invalid_request_error", MODEL_MESSAGE, "invalid_model"),
        (404, "not_found_error", MODEL_MESSAGE, "invalid_model"),
        (400, "invalid_request_error", FEATURE_MESSAGE, "unsupported_feature"),
    ],
)
def test_a_known_message_phrase_reports_its_fixed_reason_without_the_text(
    status: int,
    error_type: str,
    message: str,
    reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run(_status_error(status, error_type, message), ["--live"])

    assert code == 1
    out = capsys.readouterr().out
    assert f"reason: {reason}" in out
    assert message not in out
    assert "claude-not-a-real-model" not in out


def test_a_credit_failure_keeps_its_documented_type_and_adds_the_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run(
        _status_error(400, "invalid_request_error", CREDIT_MESSAGE),
        ["--live"],
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "category: invalid_request_error" in out
    assert "reason: insufficient_credits" in out


def test_an_unmatched_message_reports_not_classified_and_no_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run(
        _status_error(400, "invalid_request_error", "Something unexpected happened."),
        ["--live"],
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "reason: not classified" in out
    assert "Something unexpected" not in out


@pytest.mark.parametrize(
    "body",
    [
        None,
        "plain text body",
        {"error": "not a dict"},
        {"error": {"type": "invalid_request_error", "message": 42}},
    ],
)
def test_a_body_without_a_string_message_reports_not_classified(
    body: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = APIStatusError(
        "status 400",
        response=httpx.Response(400, request=REQUEST),
        body=body,
    )

    code, _ = _run(error, ["--live"])

    assert code == 1
    assert "reason: not classified" in capsys.readouterr().out


def test_a_missing_key_fails_closed_naming_only_the_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, client = _run(_ok_reply(), ["--live"], {"AEGIS_LLM_MODEL": MODEL})

    assert code == 2
    assert client is None
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out


def test_a_missing_model_fails_closed_before_any_client_is_built(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, client = _run(_ok_reply(), ["--live"], {"ANTHROPIC_API_KEY": KEY})

    assert code == 2
    assert client is None
    assert "AEGIS_LLM_MODEL" in capsys.readouterr().out


def test_an_unsafe_request_id_is_not_printed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = APIStatusError(
        "status 400",
        response=httpx.Response(
            400,
            request=REQUEST,
            headers={"request-id": "req id with spaces"},
        ),
        body=None,
    )

    code, _ = _run(error, ["--live"])

    assert code == 1
    out = capsys.readouterr().out
    assert "request_id: not provided" in out
    assert "spaces" not in out
