"""Context compaction when conversation history exceeds a token budget.

``COMPACTION_TOKEN_THRESHOLD`` (80k tokens) triggers a hidden ``compaction``
agent run on the org's ``small_model`` before the next provider step. Failures
are logged and ignored so the main harness run continues.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .images import IMAGE_TOKEN_ESTIMATE
from .providers.base import LLMMessage

#: Approximate token budget before older history is summarized (see module docstring).
COMPACTION_TOKEN_THRESHOLD = 80_000


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of a compaction pass."""

    summary: str
    messages: list[LLMMessage]
    compacted: bool


def _estimate_content_tokens(content: str | list[dict[str, Any]] | None) -> int:
    """Estimate tokens for message content (~4 chars/token for text)."""
    if not content:
        return 0
    if isinstance(content, str):
        return max(len(content) // 4, 0)
    total = 0
    for part in content:
        if part.get("type") == "text":
            total += len(str(part.get("text", ""))) // 4
        elif part.get("type") == "image_url":
            total += IMAGE_TOKEN_ESTIMATE
    return total


def estimate_message_tokens(messages: list[LLMMessage]) -> int:
    """Rough token estimate from message content and tool-call arguments."""
    total = 0
    for message in messages:
        total += _estimate_content_tokens(message.content)
        for call in message.tool_calls or ():
            total += len(str(call.get("arguments", ""))) // 4
    return max(total, 0)


def should_compact(
    messages: list[LLMMessage],
    *,
    session_tokens: dict[str, int] | None = None,
    threshold: int = COMPACTION_TOKEN_THRESHOLD,
) -> bool:
    """Return True when history plus session usage exceeds *threshold*."""
    session_total = 0
    if session_tokens:
        session_total = int(session_tokens.get("total") or 0)
    estimated = estimate_message_tokens(messages) + session_total
    return estimated >= threshold


def _format_content_for_compaction(
    content: str | list[dict[str, Any]] | None,
) -> str:
    """Serialize message content for compaction without base64 payloads."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif part.get("type") == "image_url":
            parts.append("[image]")
    return "".join(parts)


def build_compaction_prompt(messages: list[LLMMessage]) -> str:
    """Serialize *messages* (excluding system) for the compaction agent."""
    lines: list[str] = []
    for message in messages:
        if message.role == "system":
            continue
        content = _format_content_for_compaction(message.content)
        if message.tool_calls:
            for call in message.tool_calls:
                content = (
                    f"{content}\n[tool_call {call.get('name', 'tool')}: "
                    f"{call.get('arguments', '')}]"
                ).strip()
        if not content and message.role != "assistant":
            continue
        lines.append(f"{message.role.upper()}: {content}")
    return "\n\n".join(lines)


def apply_compaction_summary(
    messages: list[LLMMessage],
    summary: str,
) -> list[LLMMessage]:
    """Replace non-system history with a single summary user message."""
    system = [message for message in messages if message.role == "system"]
    if not summary.strip():
        return messages
    note = (
        "[Context compacted — earlier conversation summarized below.]\n\n"
        f"{summary.strip()}"
    )
    return [
        *system,
        LLMMessage(role="user", content=note),
    ]
