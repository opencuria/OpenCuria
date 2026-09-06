"""Transient provider-step retries (OpenCode SessionRetry-compatible).

Retries timeouts, rate limits, 5xx, and network-like failures around a
single provider stream. Context overflow and auth errors are not retried.
"""

from __future__ import annotations

import asyncio
import math
import random
import re

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


def retry_delay(attempt: int, random_value: float | None = None) -> float:
    """Return backoff delay in seconds for a 1-based retry *attempt*.

    Matches OpenCode ``SessionRetry.delay`` without Retry-After headers:
    exponential backoff from 2s, 25% jitter, ceil in milliseconds, cap 30s.
    """
    jitter = random.random() if random_value is None else random_value
    base_ms = (
        RETRY_INITIAL_DELAY * 1000 * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    )
    delay_ms = math.ceil(base_ms + base_ms * RETRY_JITTER_FACTOR * jitter)
    return min(delay_ms, RETRY_MAX_DELAY_NO_HEADERS * 1000) / 1000.0


def matches_retryable_message(value: str) -> bool:
    """Return True when *value* matches a transient provider-error pattern."""
    return any(pattern.search(value) for pattern in RETRYABLE_MESSAGE_PATTERNS)


def is_retryable_provider_error(exc: BaseException) -> bool:
    """Return True when *exc* should be retried at the provider step."""
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return False
    if is_context_overflow_error(exc):
        return False
    if isinstance(exc, ProviderAuthError):
        return False
    if isinstance(exc, ProviderTimeoutError):
        return True
    if isinstance(exc, ProviderRateLimitError):
        return True
    if isinstance(exc, ProviderResponseError):
        if exc.status_code is not None and exc.status_code >= 500:
            return True
        return matches_retryable_message(str(exc))
    return matches_retryable_message(str(exc))
