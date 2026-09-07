"""Tests for OpenCode-compatible transient provider retries."""

from __future__ import annotations

import email.utils
import time

import pytest

from apps.harness.provider_retry import (
    NON_RETRYABLE_MESSAGE_PATTERNS,
    RETRY_MAX_DELAY,
    RETRY_MAX_DELAY_NO_HEADERS,
    RETRY_MAX_RETRIES,
    is_retryable_provider_error,
    retry_delay,
)
from apps.harness.providers.base import (
    ProviderAuthError,
    ProviderHeaderTimeoutError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderStreamTimeoutError,
    ProviderTimeoutError,
)


def test_retry_delay_formula_with_fixed_jitter() -> None:
    """Attempt 1 is 2s plus 25% jitter; later attempts cap at 30s."""
    assert retry_delay(1, random_value=0.0) == 2.0
    assert retry_delay(1, random_value=1.0) == 2.5
    assert retry_delay(2, random_value=0.0) == 4.0
    assert retry_delay(5, random_value=1.0) == RETRY_MAX_DELAY_NO_HEADERS
    assert RETRY_MAX_RETRIES == 5


def test_timeouts_and_rate_limits_are_retryable() -> None:
    """OpenCode treats header/stream timeouts and 429 as retryable."""
    assert is_retryable_provider_error(ProviderTimeoutError("timed out"))
    assert is_retryable_provider_error(ProviderHeaderTimeoutError(timeout_seconds=300))
    assert is_retryable_provider_error(ProviderStreamTimeoutError())
    assert is_retryable_provider_error(ProviderRateLimitError("slow down"))
    assert is_retryable_provider_error(
        ProviderResponseError("upstream", status_code=503)
    )
    assert is_retryable_provider_error(RuntimeError("connection refused"))


def test_auth_and_overflow_are_not_retryable() -> None:
    """Auth and context overflow must not consume transient retries."""
    assert not is_retryable_provider_error(ProviderAuthError("nope"))
    assert not is_retryable_provider_error(
        RuntimeError("maximum context length exceeded")
    )
    assert not is_retryable_provider_error(
        ProviderResponseError("bad request", status_code=400)
    )


def _rate_limit(message: str, **kwargs: object) -> ProviderRateLimitError:
    """Build a rate-limit error with optional response context."""
    return ProviderRateLimitError(message, **kwargs)  # type: ignore[arg-type]


def test_retry_delay_honors_retry_after_ms() -> None:
    """retry-after-ms header is used directly (milliseconds)."""
    error = _rate_limit("slow", response_headers={"retry-after-ms": "1500"})
    assert retry_delay(1, error=error) == pytest.approx(1.5)


def test_retry_delay_honors_retry_after_seconds() -> None:
    """retry-after header in seconds is converted to a delay."""
    error = _rate_limit("slow", response_headers={"retry-after": "2"})
    assert retry_delay(1, error=error) == pytest.approx(2.0)


def test_retry_delay_honors_retry_after_http_date() -> None:
    """retry-after as HTTP-date is parsed against the current time."""
    target = time.time() + 5
    http_date = email.utils.formatdate(target, usegmt=True)
    error = _rate_limit("slow", response_headers={"retry-after": http_date})
    assert retry_delay(1, error=error) == pytest.approx(5.0, abs=1.0)


def test_retry_delay_caps_server_delay_at_max() -> None:
    """Server-requested delays are capped at RETRY_MAX_DELAY (OpenCode)."""
    assert RETRY_MAX_DELAY == 2_147_483_647
    error = _rate_limit("slow", response_headers={"retry-after-ms": "9999999999"})
    assert retry_delay(1, error=error) == pytest.approx(RETRY_MAX_DELAY / 1000.0)


def test_retry_delay_without_headers_keeps_backoff() -> None:
    """Without headers the exponential 2s backoff with 30s cap applies."""
    assert retry_delay(3, random_value=0.0) == 8.0
    assert retry_delay(9, random_value=1.0) == RETRY_MAX_DELAY_NO_HEADERS


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota: you exceeded your quota",
        "Billing hard limit reached",
        "quota exceeded for project",
        "out of budget this month",
        "FreeUsageLimitError: free tier exhausted",
        "GoUsageLimitError: plan limit reached",
        "Monthly usage limit reached, top up",
        "switch to available balance usage",
    ],
)
def test_quota_billing_errors_are_never_retried(message: str) -> None:
    """Quota/billing exhaustion fails fast, even on 429/5xx-like errors."""
    assert NON_RETRYABLE_MESSAGE_PATTERNS
    assert not is_retryable_provider_error(ProviderRateLimitError(message))
    assert not is_retryable_provider_error(
        ProviderResponseError(message, status_code=503)
    )
    assert not is_retryable_provider_error(RuntimeError(message))


def test_quota_in_response_body_is_not_retried() -> None:
    """Quota text hidden in response_body also disables retries."""
    error = ProviderResponseError(
        "upstream",
        status_code=503,
        response_body="insufficient_quota in body",
    )
    assert not is_retryable_provider_error(error)


def test_overflow_patterns_are_not_retried() -> None:
    """Pi-level overflow phrases never consume transient retries."""
    assert not is_retryable_provider_error(
        RuntimeError("prompt is too long: 213462 tokens > 200000 maximum")
    )
    assert not is_retryable_provider_error(RuntimeError("request_too_large"))
    assert not is_retryable_provider_error(
        RuntimeError("This model's maximum prompt length is 131072")
    )
    assert not is_retryable_provider_error(
        RuntimeError("Please reduce the length of the messages")
    )
    assert not is_retryable_provider_error(
        RuntimeError("The input token count (1196265) exceeds the maximum")
    )
    assert not is_retryable_provider_error(
        RuntimeError("Your request exceeded model token limit: 10 (requested: 20)")
    )
    assert not is_retryable_provider_error(RuntimeError("token limit exceeded"))
    assert not is_retryable_provider_error(
        ProviderResponseError("busy", status_code=503, response_body="too many tokens")
    )


def test_throttling_is_not_overflow_but_retryable() -> None:
    """Throttling/rate-limit prefixes are excluded from overflow detection."""
    from apps.harness.compaction import is_context_overflow_error

    throttling = RuntimeError("Throttling error: Too many tokens, please wait")
    assert not is_context_overflow_error(throttling)
    assert not is_context_overflow_error(RuntimeError("rate limit exceeded"))
    assert not is_context_overflow_error(RuntimeError("too many requests"))
    assert is_retryable_provider_error(RuntimeError("rate limit exceeded"))
    assert is_retryable_provider_error(RuntimeError("too many requests"))


def test_should_retry_header_true_forces_retry() -> None:
    """x-should-retry: true forces a retry even for a plain 400 error."""
    error = ProviderResponseError(
        "bad request",
        status_code=400,
        response_headers={"x-should-retry": "true"},
    )
    assert is_retryable_provider_error(error)


def test_should_retry_header_false_blocks_retry() -> None:
    """x-should-retry: false blocks retries even on 429/5xx errors."""
    rate_limited = ProviderRateLimitError(
        "slow down",
        status_code=429,
        response_headers={"x-should-retry": "false"},
    )
    assert not is_retryable_provider_error(rate_limited)
    server_error = ProviderResponseError(
        "upstream",
        status_code=503,
        response_headers={"X-Should-Retry": "false"},
    )
    assert not is_retryable_provider_error(server_error)


def test_status_500_and_above_retry() -> None:
    """5xx provider responses are retryable; auth never is."""
    assert is_retryable_provider_error(
        ProviderResponseError("boom", status_code=500)
    )
    assert not is_retryable_provider_error(
        ProviderAuthError("denied", status_code=401)
    )
    assert not is_retryable_provider_error(
        ProviderAuthError("forbidden", status_code=403)
    )


def test_error_fields_default_backward_compatible() -> None:
    """New error fields default to None/empty and stay mutable-safe."""
    error = ProviderResponseError("oops")
    assert error.status_code is None
    assert error.response_headers == {}
    assert error.response_body == ""
    assert error.is_retryable is None
    limited = ProviderRateLimitError("slow")
    assert limited.status_code is None
    assert limited.response_headers == {}
    assert limited.response_body == ""
    assert limited.is_retryable is None
