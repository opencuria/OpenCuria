"""Context compaction when conversation history exceeds a token budget.

``COMPACTION_TOKEN_THRESHOLD`` (80k tokens) triggers a hidden ``compaction``
agent run on the org's ``small_model`` before the next provider step. Failures
are logged and ignored so the main harness run continues.
"""

from __future__ import annotations

from dataclasses import dataclass

from .providers.base import LLMMessage

#: Approximate token budget before older history is summarized (see module docstring).
COMPACTION_TOKEN_THRESHOLD = 80_000


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of a compaction pass."""

    summary: str
    messages: list[LLMMessage]
    compacted: bool


def estimate_message_tokens(messages: list[LLMMessage]) -> int:
    """Rough token estimate from message character counts (~4 chars/token)."""
    total_chars = 0
    for message in messages:
        if message.content:
            total_chars += len(message.content)
        for call in message.tool_calls or ():
            total_chars += len(str(call.get("arguments", "")))
    return max(total_chars // 4, 0)


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


def build_compaction_prompt(messages: list[LLMMessage]) -> str:
    """Serialize *messages* (excluding system) for the compaction agent."""
    lines: list[str] = []
    for message in messages:
        if message.role == "system":
            continue
        content = message.content or ""
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
