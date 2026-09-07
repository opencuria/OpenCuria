"""
Provider adapter ABC and shared types for the agent harness.

Defines the contract every LLM provider adapter must implement:
``chat_stream`` yields incremental :class:`Delta` objects for a chat
completion request with optional tool support.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMMessage:
    """A single chat message sent to or received from a provider."""

    role: str
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    message_id: str = ""


@dataclass(frozen=True)
class ToolSchema:
    """JSON-schema style tool definition for function calling."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token usage and billed cost reported by the provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0

    def merge(self, other: Usage) -> Usage:
        """Return a new usage with token counts and cost added together."""
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost=self.cost + other.cost,
        )


@dataclass(frozen=True)
class Delta:
    """One incremental update from a streaming chat completion."""

    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    usage: Usage | None = None
    finish_reason: str | None = None


class ProviderError(Exception):
    """Base error for all provider failures."""

    def __init__(self, message: str, *, provider: str = "") -> None:
        self.provider = provider
        super().__init__(message)


class ProviderAuthError(ProviderError):
    """Raised when the provider rejects credentials (401/403)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        response_headers: dict[str, str] | None = None,
        response_body: str = "",
        is_retryable: bool | None = None,
    ) -> None:
        """Create an auth error with optional HTTP response context.

        Args:
            message: Human-readable error description.
            provider: Provider name (e.g. ``"openrouter"``).
            status_code: HTTP status code, if known.
            response_headers: Response headers (lowercased names).
            response_body: Truncated raw response body for classification.
            is_retryable: Provider retry hint (``None`` when unknown).
        """
        self.status_code = status_code
        self.response_headers: dict[str, str] = dict(response_headers or {})
        self.response_body = response_body
        self.is_retryable = is_retryable
        super().__init__(message, provider=provider)


class ProviderRateLimitError(ProviderError):
    """Raised when the provider rate-limits the request (429)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        response_headers: dict[str, str] | None = None,
        response_body: str = "",
        is_retryable: bool | None = None,
    ) -> None:
        """Create a rate-limit error with optional HTTP response context.

        Args:
            message: Human-readable error description.
            provider: Provider name (e.g. ``"openrouter"``).
            status_code: HTTP status code, if known.
            response_headers: Response headers (lowercased names).
            response_body: Truncated raw response body for classification.
            is_retryable: Provider retry hint (``None`` when unknown).
        """
        self.status_code = status_code
        self.response_headers: dict[str, str] = dict(response_headers or {})
        self.response_body = response_body
        self.is_retryable = is_retryable
        super().__init__(message, provider=provider)


class ProviderTimeoutError(ProviderError):
    """Raised when the provider request times out."""


class ProviderHeaderTimeoutError(ProviderTimeoutError):
    """Raised when response headers do not arrive in time."""

    def __init__(
        self,
        message: str = "",
        *,
        provider: str = "",
        timeout_seconds: float = 0.0,
    ) -> None:
        ms = int(timeout_seconds * 1000)
        super().__init__(
            message or f"Provider response headers timed out after {ms}ms",
            provider=provider,
        )
        self.timeout_seconds = timeout_seconds


class ProviderStreamTimeoutError(ProviderTimeoutError):
    """Raised when the SSE stream goes idle between chunks."""

    def __init__(self, message: str = "", *, provider: str = "") -> None:
        super().__init__(message or "SSE read timed out", provider=provider)


class ProviderResponseError(ProviderError):
    """Raised for malformed or unexpected provider responses."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        response_headers: dict[str, str] | None = None,
        response_body: str = "",
        is_retryable: bool | None = None,
    ) -> None:
        """Create a response error with optional HTTP response context.

        Args:
            message: Human-readable error description.
            provider: Provider name (e.g. ``"openrouter"``).
            status_code: HTTP status code, if known.
            response_headers: Response headers (lowercased names).
            response_body: Truncated raw response body for classification.
            is_retryable: Provider retry hint (``None`` when unknown).
        """
        self.status_code = status_code
        self.response_headers: dict[str, str] = dict(response_headers or {})
        self.response_body = response_body
        self.is_retryable = is_retryable
        super().__init__(message, provider=provider)


#: OpenCode ``headerTimeout`` default (ms → seconds).
DEFAULT_HEADER_TIMEOUT_SECONDS = 300.0

#: OpenCode ``chunkTimeout`` default (ms → seconds).
DEFAULT_CHUNK_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ChatOptions:
    """Optional per-request settings for a chat completion.

    Timeouts match OpenCode's fetch wrapper: wait up to
    ``header_timeout_seconds`` for response headers, then up to
    ``chunk_timeout_seconds`` of idle time between SSE chunks. ``None``
    or ``<= 0`` disables that phase. ``timeout_seconds`` is an optional
    wall-clock cap with no default.
    """

    temperature: float | None = None
    max_tokens: int | None = None
    header_timeout_seconds: float | None = DEFAULT_HEADER_TIMEOUT_SECONDS
    chunk_timeout_seconds: float | None = DEFAULT_CHUNK_TIMEOUT_SECONDS
    timeout_seconds: float | None = None
    reasoning_effort: str | None = None
    tool_choice: str | None = None


class ProviderAdapter(abc.ABC):
    """Abstract interface every LLM provider adapter must implement."""

    name: str = "base"

    @abc.abstractmethod
    def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Stream a chat completion as incremental deltas.

        Args:
            model: Provider model identifier.
            messages: Conversation history.
            tools: Tools available for function calling.
            opts: Optional request settings.

        Yields:
            Incremental :class:`Delta` objects.

        Raises:
            ProviderError: On auth, rate-limit, timeout, or
                response failures.
        """
        raise NotImplementedError
        yield Delta()  # pragma: no cover - keeps the method an iterator
