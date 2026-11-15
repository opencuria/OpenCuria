"""Characterize shared provider context-error classification."""

import pytest

from apps.harness.providers.base import (
    ProviderAuthError,
    ProviderHeaderTimeoutError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderStreamTimeoutError,
    ProviderTimeoutError,
)
from apps.harness.providers.context_errors import is_context_overflow_error


@pytest.mark.parametrize(
    "error_type",
    [
        ProviderAuthError,
        ProviderRateLimitError,
        ProviderTimeoutError,
        ProviderHeaderTimeoutError,
        ProviderStreamTimeoutError,
    ],
)
def test_typed_failures_take_precedence_over_overflow_text(
    error_type: type[Exception],
) -> None:
    """Auth, throttling and timeout types never trigger compaction."""
    assert not is_context_overflow_error(error_type("context_length_exceeded"))


def test_overflow_in_response_body_is_detected() -> None:
    """An opaque HTTP message must not hide overflow in the response body."""
    error = ProviderResponseError(
        "Bad request",
        response_body='{"error": "INPUT IS TOO LONG FOR REQUESTED MODEL"}',
    )
    assert is_context_overflow_error(error)


@pytest.mark.parametrize(
    ("message", "body"),
    [
        ("context_length_exceeded", "rate limit exceeded"),
        ("rate limit exceeded", "context_length_exceeded"),
    ],
)
def test_rate_limit_exclusions_apply_to_message_and_body(
    message: str, body: str
) -> None:
    """A rate-limit exclusion wins even when the other field says overflow."""
    error = ProviderResponseError(message, response_body=body)
    assert not is_context_overflow_error(error)


def test_is_context_overflow_error_matches_phrases() -> None:
    """Provider context errors are detected by message substring."""
    assert is_context_overflow_error(RuntimeError("maximum context length exceeded"))
    assert is_context_overflow_error(ValueError("prompt is too long: 999 > 100"))
    assert not is_context_overflow_error(RuntimeError("rate limit exceeded"))


def test_is_context_overflow_error_skips_rate_limit_auth_timeout() -> None:
    """Typed rate-limit/auth/timeout errors are never treated as overflow."""
    daily = "Too many tokens per day, please wait before trying again."
    assert not is_context_overflow_error(ProviderRateLimitError(daily))
    assert not is_context_overflow_error(
        ProviderRateLimitError("slow", response_body="prompt is too long")
    )
    assert not is_context_overflow_error(ProviderAuthError("unauthorized"))
    assert not is_context_overflow_error(ProviderTimeoutError("timed out"))
    assert is_context_overflow_error(RuntimeError("too many tokens in request"))


def test_is_context_overflow_error_pi_patterns() -> None:
    """Pi OVERFLOW_PATTERNS mark overflow; NON_OVERFLOW exclusions win."""
    positives = [
        "prompt is too long: 213462 tokens > 200000 maximum",
        "request_too_large: request exceeds the maximum size",
        "Your input exceeds the context window of this model",
        "Requested token count exceeds the model's maximum context length",
        "The input token count (1196265) exceeds the maximum number",
        "This model's maximum prompt length is 131072",
        "Please reduce the length of the messages or completion",
        "This endpoint's maximum context length is 100000 tokens",
        "Input length 10 exceeds the maximum allowed input length of 5",
        "The input (10 tokens) is longer than the model's context length",
        "the request exceeds the available context size",
        "tokens to keep is greater than the context length",
        "context window exceeds limit",
        "Your request exceeded model token limit: 10 (requested: 20)",
        "Prompt contains 10 tokens too large for model with 5 maximum",
        "Prompt has 10 tokens, but the configured context size is 5 tokens",
        "model_context_window_exceeded",
        "prompt too long; exceeded max context length by 10 tokens",
        "Range of input length should be [1, 100]",
        "context_length_exceeded",
        "too many tokens in request",
        "token limit exceeded for model",
        "400 (no body)",
        "413 status code (no body)",
    ]
    for message in positives:
        assert is_context_overflow_error(RuntimeError(message)), message
    negatives = [
        "Throttling error: Too many tokens, please wait before trying again.",
        "Service unavailable: Too many tokens, try later.",
        "rate limit exceeded, slow down",
        "too many requests, retry later",
        "Too many tokens per day, please wait before trying again.",
        "An error occurred (ThrottlingException) when calling the "
        "ConverseStream operation: Too many tokens per day",
    ]
    for message in negatives:
        assert not is_context_overflow_error(RuntimeError(message)), message
    assert is_context_overflow_error(RuntimeError("too many tokens in request"))


def test_is_context_overflow_opencode_parity() -> None:
    """Shared overflow classification matches OpenCode provider-error.ts patterns."""
    positives = [
        "Input is too long for requested model",
        "Prompt has 100 tokens, but the configured context size is 50 tokens",
        "input (10 tokens) is longer than the model's context length (5 tokens)",
        "tokens in request more than max tokens allowed",
        "request entity too large",
        "This model's maximum context length is 200000 tokens (of 100 tokens)",
        "exceeds the limit of 100",
        "context length is only 10 tokens",
        "input length 10 exceeds the context length 5",
        "prompt too long; exceeded max context length",
        "too large for model with 5 maximum context length",
        "This request exceeds the context window",
        "model_context_window_exceeded",
        "400 (no body)",
        "413 status code (no body)",
    ]
    for message in positives:
        assert is_context_overflow_error(RuntimeError(message)), message
    negatives = [
        "Throttling error: slow down",
        "Service unavailable: busy",
        "rate limit exceeded",
        "too many requests, retry later",
        "Too many tokens per day, please wait before trying again.",
    ]
    for message in negatives:
        assert not is_context_overflow_error(RuntimeError(message)), message
