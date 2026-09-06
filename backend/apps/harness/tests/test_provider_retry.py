"""Tests for OpenCode-compatible transient provider retries."""

from __future__ import annotations

from apps.harness.provider_retry import (
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
