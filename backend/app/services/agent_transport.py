"""Budgeted transport shared by every live provider adapter.

Each live adapter supplies two things: a function that sends one request, and a
function that classifies the errors that request can raise. This module does the
rest, so the budget rules are written once and apply to every provider the same way.

For each attempt, including a retry:

- The budget admits the request, or refuses it before anything is sent.
- The per-request timeout is taken from the time left before the deadline. If no
  time is left, nothing is sent.
- The attempt is counted as a request, whatever it returns.

A retry happens only if the retry limit allows it, the error is transient, and the
backoff still ends before the deadline. Any SDK message is dropped. Only the fixed
failure reason reaches the caller.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.domain.agent_records import PROVIDER_REQUEST_ID_PATTERN, ProviderFailureReason
from app.services.agent_budget import RunBudget, estimate_tokens, serialize_request
from app.services.agent_provider import ProviderError

RETRY_BASE_SECONDS = 0.5
RETRY_CAP_SECONDS = 4.0

SendRequest = Callable[[dict[str, Any], float], Any]


@dataclass(frozen=True)
class FailureClass:
    """How one SDK error is reported: a fixed reason, whether it is transient, and
    the safe HTTP status and request ID when the error carries them."""

    reason: ProviderFailureReason
    retryable: bool
    http_status: int | None = None
    request_id: str | None = None


ClassifyError = Callable[[Exception], FailureClass]


def safe_request_id(value: object) -> str | None:
    """Keep a provider request ID only if it has the expected shape.

    The ID comes from a response header. Anything else is dropped, so a provider
    cannot place free text in the result through this field.
    """
    if isinstance(value, str) and re.fullmatch(PROVIDER_REQUEST_ID_PATTERN, value):
        return value
    return None


def send_within_budget(
    request: dict[str, Any],
    budget: RunBudget,
    *,
    send: SendRequest,
    classify: ClassifyError,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Send one request under the budget, retrying only as the limits allow.

    ``send`` receives the request and the timeout for this attempt. ``classify``
    maps an exception to a fixed reason, to whether it is transient, and to the safe
    diagnostics. Raises ``ProviderError``, with reason ``BUDGET`` when a limit
    stopped the request.
    """
    body = serialize_request(request)
    estimate = estimate_tokens(body)
    attempt = 0
    while True:
        attempt += 1
        if budget.admit_request(estimate, serialized_chars=len(body)) is not None:
            raise ProviderError(ProviderFailureReason.BUDGET)
        timeout = budget.request_timeout()
        if timeout is None:
            raise ProviderError(ProviderFailureReason.BUDGET)
        budget.record_request()
        try:
            return send(request, timeout)
        except Exception as error:
            failure = classify(error)

        delay = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** (attempt - 1))
        can_retry = (
            failure.retryable
            and attempt <= budget.limits.max_retries
            and delay < budget.remaining_seconds()
        )
        if not can_retry:
            raise ProviderError(
                failure.reason,
                http_status=failure.http_status,
                request_id=failure.request_id,
            )
        sleep(delay)
