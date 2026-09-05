"""Context compaction when conversation history exceeds the model budget.

Overflow is derived from the last provider step token usage and the
resolved model context limits (OpenCode-compatible formulas). When
``is_overflow`` is true, a hidden ``compaction`` agent run on the session
model summarizes older history before the next provider step. Failures
are logged and ignored so the main harness run continues.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .images import IMAGE_TOKEN_ESTIMATE
from .providers.base import LLMMessage

#: Reserved headroom hint for future compaction selection (not used in usable).
COMPACTION_BUFFER = 20_000

#: Cap on resolved max output tokens (matches OpenCode).
OUTPUT_TOKEN_MAX = 32_000

MIN_PRESERVE_RECENT_TOKENS = 2_000
MAX_PRESERVE_RECENT_TOKENS = 15_000

TOOL_OUTPUT_MAX_CHARS = 2_000

#: Prefix for in-memory checkpoint user messages (not persisted as HarnessMessage).
CHECKPOINT_PREFIX = "[opencuria-compaction-checkpoint]"

SUMMARY_TEMPLATE = """Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers when known.
- Do not mention the summary process or that context was compacted."""

SUMMARY_UPDATE_INSTRUCTIONS = """The <prior-summary> summarizes everything that happened before the <conversation>. Construct a new summary that combines both. The <prior-summary> is discarded after this: anything you do not carry into the new summary is lost.

When combining:
- Carry forward objectives, constraints, user directives, decisions, and parallel workstreams from the <prior-summary> even when the <conversation> does not mention them. Drop only what is finished and no longer needed.
- The <conversation> is more recent than the <prior-summary>. Where they conflict, the conversation wins: state the corrected fact and drop the old claim.
- Add new progress, decisions, constraints, and context from the conversation.
- Move completed work from "Active" to "Completed".
- If a blocker has been resolved, update the summary to reflect that while keeping any details still needed to continue the work.
- Update "Objective" and "Next Move" to reflect the current work state."""

_CONTEXT_OVERFLOW_PHRASES = (
    "context length",
    "prompt is too long",
    "maximum context",
    "too many tokens",
)


@dataclass(frozen=True)
class ModelLimits:
    """Provider model context limits from the org catalog."""

    context_length: int = 0
    max_output_tokens: int = 0


@dataclass(frozen=True)
class CompactionSelection:
    """Head/tail split for summarization."""

    head: list[LLMMessage]
    tail: list[LLMMessage]
    tail_start_id: str


def max_output_tokens(limits: ModelLimits) -> int:
    """Resolve capped max output tokens (OpenCode ``maxOutputTokens``)."""
    return min(limits.max_output_tokens, OUTPUT_TOKEN_MAX) or OUTPUT_TOKEN_MAX


def usable(limits: ModelLimits) -> int:
    """Return usable input token budget for overflow checks."""
    if limits.context_length == 0:
        return 0
    return max(0, limits.context_length - max_output_tokens(limits))


def token_count(prompt: int, completion: int, total: int = 0) -> int:
    """Return the token count for one provider step."""
    if total > 0:
        return total
    return prompt + completion


def is_overflow(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int = 0,
    limits: ModelLimits,
    auto: bool = True,
) -> bool:
    """Return True when the last step exceeds the model's usable context."""
    if not auto:
        return False
    if limits.context_length == 0:
        return False
    count = token_count(prompt_tokens, completion_tokens, total_tokens)
    return count >= usable(limits)


def preserve_recent_budget(limits: ModelLimits) -> int:
    """Token budget reserved for recent tail turns during compaction."""
    budget = usable(limits)
    return min(
        MAX_PRESERVE_RECENT_TOKENS,
        max(MIN_PRESERVE_RECENT_TOKENS, math.floor(budget * 0.25)),
    )


def is_context_overflow_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a provider context-length failure."""
    text = str(exc).lower()
    return any(phrase in text for phrase in _CONTEXT_OVERFLOW_PHRASES)


def _truncate(value: str) -> str:
    """Truncate tool output for compaction serialization."""
    if len(value) <= TOOL_OUTPUT_MAX_CHARS:
        return value
    return f"{value[:TOOL_OUTPUT_MAX_CHARS]}\n[truncated]"


def _is_checkpoint(message: LLMMessage) -> bool:
    """Return True when *message* is an in-memory compaction checkpoint."""
    if message.role != "user" or not isinstance(message.content, str):
        return False
    return message.content.startswith(CHECKPOINT_PREFIX)


def _checkpoint_summary(message: LLMMessage) -> str | None:
    """Extract summary text from a checkpoint user message."""
    if not _is_checkpoint(message) or not isinstance(message.content, str):
        return None
    body = message.content[len(CHECKPOINT_PREFIX) :].lstrip("\n").strip()
    return body or None


def find_previous_summary(messages: list[LLMMessage]) -> str | None:
    """Return the latest checkpoint summary from *messages*, if any."""
    for message in reversed(messages):
        summary = _checkpoint_summary(message)
        if summary:
            return summary
    return None


def _format_user_content(content: str | list[dict[str, Any]] | None) -> str:
    """Serialize user content for compaction (images become ``[image]``)."""
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


def serialize(message: LLMMessage) -> str:
    """Serialize one message for the compaction prompt (OpenCode-style)."""
    if message.role == "system":
        return ""
    if message.role == "user":
        text = _format_user_content(message.content)
        lines = [f"[User]: {text}"] if text else []
        return "\n".join(lines)
    if message.role == "assistant":
        lines: list[str] = []
        content = _format_user_content(message.content)
        if content:
            lines.append(f"[Assistant]: {content}")
        for call in message.tool_calls or ():
            name = str(call.get("name", "tool") or "tool")
            args = call.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args)
            lines.append(f"[Assistant tool call]: {name}({args})")
        return "\n".join(lines)
    if message.role == "tool":
        output = _truncate(str(message.content or ""))
        return f"[Tool result]: {output}"
    return ""


def _build_prompt(
    *,
    previous_summary: str | None = None,
    context: list[str],
) -> str:
    """Build the compaction user prompt (OpenCode ``buildPrompt``)."""
    conversation = (
        "Here is the conversation so far:\n\n"
        f"<conversation>\n{'\n\n'.join(context)}\n</conversation>"
    )
    if not previous_summary:
        return "\n\n".join(
            [
                conversation,
                (
                    "Create a new anchored summary from the conversation "
                    "history in the <conversation> tags above so another "
                    "coding agent can continue the work."
                ),
                SUMMARY_TEMPLATE,
            ]
        )
    prior = (
        "Here is the summary of the conversation before the <conversation> "
        f"above:\n\n<prior-summary>\n{previous_summary}\n</prior-summary>"
    )
    return "\n\n".join([conversation, prior, SUMMARY_UPDATE_INSTRUCTIONS, SUMMARY_TEMPLATE])


def build_compaction_prompt(
    head: list[LLMMessage],
    *,
    previous_summary: str | None = None,
) -> str:
    """Build the compaction agent user prompt from *head* messages."""
    context = [text for text in (serialize(message) for message in head) if text]
    if not context and not previous_summary:
        return ""
    return _build_prompt(previous_summary=previous_summary, context=context)


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


def _conversation_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Return non-system messages from *messages*."""
    return [message for message in messages if message.role != "system"]


def _turn_starts(conversation: list[LLMMessage]) -> list[int]:
    """Return indices of real (non-checkpoint) user turns."""
    return [
        index
        for index, message in enumerate(conversation)
        if message.role == "user" and not _is_checkpoint(message)
    ]


def _split_turn(
    conversation: list[LLMMessage],
    turn_start: int,
    turn_end: int,
    budget: int,
) -> int | None:
    """Return a later start index within a turn that fits *budget*."""
    if budget <= 0 or turn_end - turn_start <= 1:
        return None
    for start in range(turn_start + 1, turn_end):
        size = estimate_message_tokens(conversation[start:turn_end])
        if size <= budget:
            return start
    return None


def select(messages: list[LLMMessage], limits: ModelLimits) -> CompactionSelection:
    """Split *messages* into summarizable head and preserved tail."""
    conversation = _conversation_messages(messages)
    starts = _turn_starts(conversation)
    if not starts:
        return CompactionSelection(head=conversation, tail=[], tail_start_id="")

    budget = preserve_recent_budget(limits)
    keep_start: int | None = None
    total = 0

    for index in range(len(starts) - 1, -1, -1):
        turn_start = starts[index]
        turn_end = starts[index + 1] if index + 1 < len(starts) else len(conversation)
        size = estimate_message_tokens(conversation[turn_start:turn_end])
        if total + size <= budget:
            total += size
            keep_start = turn_start
            continue
        remaining = budget - total
        split = _split_turn(conversation, turn_start, turn_end, remaining)
        if split is not None:
            keep_start = split
        break

    last_user_start = starts[-1]
    if keep_start is None or keep_start == 0 or keep_start > last_user_start:
        keep_start = last_user_start

    if keep_start == 0:
        tail_start_id = ""
        for message in conversation:
            if message.role == "user" and not _is_checkpoint(message):
                tail_start_id = message.message_id
                break
        return CompactionSelection(head=[], tail=conversation, tail_start_id=tail_start_id)

    head = conversation[:keep_start]
    tail = conversation[keep_start:]
    tail_start_id = ""
    for message in tail:
        if message.role == "user" and not _is_checkpoint(message):
            tail_start_id = message.message_id
            break
    return CompactionSelection(head=head, tail=tail, tail_start_id=tail_start_id)


def _checkpoint_message(summary: str) -> LLMMessage:
    """Build the in-memory checkpoint user message."""
    return LLMMessage(
        role="user",
        content=f"{CHECKPOINT_PREFIX}\n{summary.strip()}",
    )


def apply_compaction(
    messages: list[LLMMessage],
    summary: str,
    limits: ModelLimits,
) -> tuple[list[LLMMessage], str]:
    """Replace summarizable history with a checkpoint and preserved tail."""
    if not summary.strip():
        return messages, ""
    system = [message for message in messages if message.role == "system"]
    selection = select(messages, limits)
    if not selection.head:
        return messages, selection.tail_start_id
    tail = [message for message in selection.tail if not _is_checkpoint(message)]
    return (
        [*system, _checkpoint_message(summary), *tail],
        selection.tail_start_id,
    )
