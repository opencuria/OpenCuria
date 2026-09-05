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


class ProviderRateLimitError(ProviderError):
    """Raised when the provider rate-limits the request (429)."""


class ProviderTimeoutError(ProviderError):
    """Raised when the provider request times out."""


class ProviderResponseError(ProviderError):
    """Raised for malformed or unexpected provider responses."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, provider=provider)


@dataclass(frozen=True)
class ChatOptions:
    """Optional per-request settings for a chat completion."""

    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float = 60.0
    reasoning_effort: str | None = None


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
