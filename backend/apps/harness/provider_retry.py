"""Transient provider-step retries (OpenCode SessionRetry-compatible).

Retries timeouts, rate limits, 5xx, and network-like failures around a
single provider stream. Context overflow and auth errors are not retried.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
import re
import time

from .compaction import is_context_overflow_error
from .providers.base import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)

RETRY_INITIAL_DELAY = 2.0
RETRY_BACKOFF_FACTOR = 2
RETRY_JITTER_FACTOR = 0.25
RETRY_MAX_DELAY_NO_HEADERS = 30.0
RETRY_MAX_RETRIES = 5

#: Retry-After cap in ms, matching OpenCode ``MAX_DELAY_MS`` in executor.ts.
RETRY_MAX_DELAY = 2_147_483_647

RETRYABLE_MESSAGE_PATTERNS = (
    re.compile(r"429|500|502|503|504|524", re.I),
    re.compile(
        r"rate increased too quickly|rate limit|rate-limit|rate_limit|"
        r"too many requests",
        re.I,
    ),
    re.compile(
        r"overloaded|service unavailable|service_unavailable|"
        r"service-unavailable|internal error|internal_error|"
        r"internal server error|server error|server_error|server-error|"
        r"provider returned error|provider_returned_error|"
        r"provider-returned-error",
        re.I,
    ),
    re.compile(
        r"terminated|fetch failed|failed to fetch|network[-_\s]error|"
        r"upstream connect|connection error|connection refused|"
        r"connection lost|socket connection was closed|socket hang up|"
        r"reset before headers|getaddrinfo|enotfound|eai_again|"
        r"econnrefused|econnreset|etimedout",
        re.I,
    ),
    re.compile(
        r"^timeout$|\b(?:request|response|connection|network|stream|read) "
        r"(?:timeout|timed out|time out)\b",
        re.I,
    ),
    re.compile(
        r"try your request again|retry your request|resource exhausted|"
        r"resource_exhausted",
        re.I,
    ),
    re.compile(
        r"\btry again (?:later|in\b)|\b(?:currently|temporarily) at capacity\b",
        re.I,
    ),
)

#: Quota/billing exhaustion is never retried (Pi NON_RETRYABLE parity).
NON_RETRYABLE_MESSAGE_PATTERNS = (
    re.compile(r"insufficient_quota", re.I),
    re.compile(r"quota exceeded", re.I),
    re.compile(r"out of budget", re.I),
    re.compile(r"billing", re.I),
    re.compile(r"FreeUsageLimitError"),
    re.compile(r"GoUsageLimitError"),
    re.compile(r"Monthly usage limit reached", re.I),
    re.compile(r"available balance", re.I),
)


def _retry_after_delay_ms(headers: dict[str, str]) -> float | None:
    """Parse Retry-After headers to a delay in ms, capped at RETRY_MAX_DELAY."""
    lowered = {str(name).lower(): value for name, value in headers.items()}
    raw_ms = lowered.get("retry-after-ms")
    if raw_ms not in (None, ""):
        try:
            value = float(str(raw_ms))
        except (TypeError, ValueError):
            value = float("nan")
        if math.isfinite(value):
            return min(max(0.0, value), float(RETRY_MAX_DELAY))
    raw = lowered.get("retry-after")
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        seconds = float("nan")
    if math.isfinite(seconds):
        return min(max(0.0, seconds * 1000.0), float(RETRY_MAX_DELAY))
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    timestamp = parsed.timestamp()
    now = time.time()
    if math.isnan(timestamp):
        return None
    return min(max(0.0, (timestamp - now) * 1000.0), float(RETRY_MAX_DELAY))


def retry_delay(
    attempt: int,
    random_value: float | None = None,
    error: BaseException | None = None,
) -> float:
    """Return backoff delay in seconds for a 1-based retry *attempt*.

    When *error* carries ``response_headers`` with ``retry-after-ms`` or
    ``retry-after`` (seconds or HTTP-date, OpenCode/Pi parity), the parsed
    server delay is honored and capped at ``RETRY_MAX_DELAY``. Otherwise
    falls back to exponential backoff from 2s with 25% jitter, cap 30s.
    """
    headers = getattr(error, "response_headers", None) if error is not None else None
    if isinstance(headers, dict) and headers:
        parsed_ms = _retry_after_delay_ms(headers)
        if parsed_ms is not None:
            return parsed_ms / 1000.0
    jitter = random.random() if random_value is None else random_value
    base_ms = RETRY_INITIAL_DELAY * 1000 * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    delay_ms = math.ceil(base_ms + base_ms * RETRY_JITTER_FACTOR * jitter)
    return min(delay_ms, RETRY_MAX_DELAY_NO_HEADERS * 1000) / 1000.0


def matches_retryable_message(value: str) -> bool:
    """Return True when *value* matches a transient provider-error pattern."""
    return any(pattern.search(value) for pattern in RETRYABLE_MESSAGE_PATTERNS)


def matches_non_retryable_message(value: str) -> bool:
    """Return True when *value* signals quota/billing exhaustion."""
    return any(pattern.search(value) for pattern in NON_RETRYABLE_MESSAGE_PATTERNS)


def _error_text(exc: BaseException) -> str:
    """Combine message and response body for error classification."""
    parts = [str(exc)]
    body = getattr(exc, "response_body", "")
    if isinstance(body, str) and body:
        parts.append(body)
    return "\n".join(parts)


def _should_retry_hint(exc: BaseException) -> bool | None:
    """Return the provider ``x-should-retry`` hint, if present (Pi parity)."""
    headers = getattr(exc, "response_headers", None)
    if not isinstance(headers, dict):
        return None
    lowered = {str(name).lower(): value for name, value in headers.items()}
    hint = str(lowered.get("x-should-retry", "")).strip().lower()
    if hint == "true":
        return True
    if hint == "false":
        return False
    return None


def is_retryable_provider_error(exc: BaseException) -> bool:
    """Return True when *exc* should be retried at the provider step."""
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return False
    text = _error_text(exc)
    if matches_non_retryable_message(text):
        return False
    if is_context_overflow_error(exc):
        return False
    if isinstance(exc, ProviderAuthError):
        return False
    if isinstance(exc, ProviderTimeoutError):
        return True
    if isinstance(exc, ProviderRateLimitError):
        hint = _should_retry_hint(exc)
        if hint is not None:
            return hint
        if matches_non_retryable_message(text):
            return False
        return True
    if isinstance(exc, ProviderResponseError):
        hint = _should_retry_hint(exc)
        if hint is not None:
            return hint
        if exc.status_code is not None and exc.status_code >= 500:
            return True
        return matches_retryable_message(text)
    hint = _should_retry_hint(exc)
    if hint is not None:
        return hint
    return matches_retryable_message(text)
