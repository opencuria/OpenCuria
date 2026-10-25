"""Classify provider context-window failures independently of run policy.

Wire adapters, retries and compaction share these rules. This module only
depends on provider error types; it never loads harness orchestration,
workspace access or persistence.
"""

from __future__ import annotations

import re

from .base import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

_CONTEXT_OVERFLOW_PHRASES = (
    "context length",
    "prompt is too long",
    "maximum context",
    "too many tokens",
)

#: OpenCode ``provider-error.ts`` parity overflow patterns
#: (case-insensitive regexes, 28 patterns + 400/413-no-body).
CONTEXT_OVERFLOW_PATTERNS = (
    re.compile(r"prompt is too long", re.I),
    re.compile(r"request_too_large", re.I),
    re.compile(r"input is too long for requested model", re.I),
    re.compile(r"exceeds the context window", re.I),
    re.compile(
        r"exceeds (?:the )?(?:model'?s )?maximum context length"
        r"(?: of [\d,]+ tokens?|\s*\([\d,]+\))?",
        re.I,
    ),
    re.compile(r"input token count.*exceeds the maximum", re.I),
    re.compile(r"tokens in request more than max tokens allowed", re.I),
    re.compile(r"maximum prompt length is \d+", re.I),
    re.compile(r"reduce the length of the messages", re.I),
    re.compile(r"maximum context length is \d+ tokens", re.I),
    re.compile(
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",
        re.I,
    ),
    re.compile(
        r"input \(\d+ tokens\) is longer than the model'?s context length"
        r" \(\d+ tokens\)",
        re.I,
    ),
    re.compile(r"exceeds the limit of \d+", re.I),
    re.compile(r"exceeds the available context size", re.I),
    re.compile(r"greater than the context length", re.I),
    re.compile(r"context window exceeds limit", re.I),
    re.compile(r"exceeded model token limit", re.I),
    re.compile(r"context[_ ]length[_ ]exceeded", re.I),
    re.compile(r"request entity too large", re.I),
    re.compile(r"context length is only \d+ tokens", re.I),
    re.compile(r"input length.*exceeds.*context length", re.I),
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.I),
    re.compile(r"too large for model with \d+ maximum context length", re.I),
    re.compile(
        r"prompt has [\d,]+ tokens?, but the configured context size is"
        r" [\d,]+ tokens?",
        re.I,
    ),
    re.compile(r"model_context_window_exceeded", re.I),
    re.compile(r"too many tokens", re.I),
    re.compile(r"token limit exceeded", re.I),
    re.compile(r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.I),
    # Pi-legacy broad fallbacks (superset of the strict OpenCode literals
    # above so older provider messages keep classifying as overflow).
    re.compile(r"exceeds (?:the )?maximum allowed input length", re.I),
    re.compile(r"too large for model", re.I),
    re.compile(r"range of input length", re.I),
)

#: Non-overflow exclusions checked before overflow patterns (Pi parity
#: plus boto3/AWS wording OpenCode's AI SDK prefix does not cover).
NON_OVERFLOW_EXCLUSIONS = (
    re.compile(r"^(throttling error|service unavailable):", re.I),
    re.compile(r"throttlingexception", re.I),
    re.compile(r"\bthrottling\b", re.I),
    re.compile(r"tokens per day", re.I),
    re.compile(r"rate limit", re.I),
    re.compile(r"too many requests", re.I),
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a provider context-length failure.

    Rate-limit, auth, and timeout errors are never overflow: Bedrock
    ``ThrottlingException: Too many tokens per day`` must not compact.
    Both the message and an enriched ``response_body`` (see
    ``providers.base``) are classified, so overflow text hidden in the
    raw body is detected as well.
    """
    if isinstance(
        exc, (ProviderRateLimitError, ProviderAuthError, ProviderTimeoutError)
    ):
        return False
    text = str(exc)
    body = getattr(exc, "response_body", "")
    if isinstance(body, str) and body:
        text = f"{text}\n{body}"
    if any(pattern.search(text) for pattern in NON_OVERFLOW_EXCLUSIONS):
        return False
    if any(pattern.search(text) for pattern in CONTEXT_OVERFLOW_PATTERNS):
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in _CONTEXT_OVERFLOW_PHRASES)
