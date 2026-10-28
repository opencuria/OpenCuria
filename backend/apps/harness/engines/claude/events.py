"""Claude SDK message parsing and normalized stream-event helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ContentBlock

from ...providers.base import Usage


@dataclass
class StepState:
    """Streaming state for API-message steps in one root or delegated scope."""

    step: int = 0
    started: bool = False
    active_calls: set[str] = field(default_factory=set)
    awaiting_next_step: bool = False
    streamed: dict[tuple[str, int, str], str] = field(default_factory=dict)
    current_message_id: str = ""
    block_types: dict[tuple[str, int], str] = field(default_factory=dict)
    block_order: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    completed_blocks: set[tuple[str, int, str]] = field(default_factory=set)
    completed_messages: set[str] = field(default_factory=set)
    usage_by_message: dict[str, Usage] = field(default_factory=dict)
    finished_usage: Usage = field(default_factory=Usage)
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)


class ClaudeEventNormalizer:
    """Stateless conversion from Claude SDK payloads to harness primitives."""

    @classmethod
    def stream_usage(cls, message: StreamEvent) -> tuple[str, Usage] | None:
        """Read an API message usage snapshot from stream boundaries."""
        raw = message.event
        if raw.get("type") == "message_start":
            api_message = raw.get("message")
            usage = api_message.get("usage") if isinstance(api_message, dict) else None
            message_id = (
                str(api_message.get("id") or "")
                if isinstance(api_message, dict)
                else ""
            )
        elif raw.get("type") == "message_delta":
            usage = raw.get("usage")
            message_id = ""
        else:
            return None
        mapped = cls.message_usage(usage)
        return (message_id, mapped) if mapped is not None else None

    @staticmethod
    def message_usage(raw: Any) -> Usage | None:
        """Map one API message's token usage, including cache token fields."""
        if not isinstance(raw, dict):
            return None

        def count(key: str) -> int:
            try:
                return int(raw.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        prompt = (
            count("input_tokens")
            + count("cache_read_input_tokens")
            + count("cache_creation_input_tokens")
        )
        completion = count("output_tokens")
        if not any(
            key in raw
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            )
        ):
            return None
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )

    @staticmethod
    def stream_block_start(message: StreamEvent) -> tuple[int, str, str, str] | None:
        """Return content-block index, type, tool name and tool-use ID."""
        raw = message.event
        if raw.get("type") != "content_block_start":
            return None
        try:
            index = int(raw.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        block = raw.get("content_block")
        block = block if isinstance(block, dict) else {}
        return (
            index,
            str(block.get("type") or ""),
            str(block.get("name") or ""),
            str(block.get("id") or ""),
        )

    @staticmethod
    def stream_block_stop(message: StreamEvent) -> int | None:
        """Return the index of a completed content block, if present."""
        raw = message.event
        if raw.get("type") != "content_block_stop":
            return None
        try:
            return int(raw.get("index", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def partial_delta(message: StreamEvent) -> tuple[int, str, str, str] | None:
        """Return index, slot, field and text for a content delta."""
        raw = message.event
        if raw.get("type") != "content_block_delta":
            return None
        delta = raw.get("delta")
        if not isinstance(delta, dict):
            return None
        kind = str(delta.get("type") or "")
        if kind == "text_delta":
            text, slot, field_name = str(delta.get("text") or ""), "text", "text"
        elif kind == "thinking_delta":
            text, slot, field_name = (
                str(delta.get("thinking") or ""),
                "thinking",
                "reasoning",
            )
        else:
            return None
        try:
            index = int(raw.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        return index, slot, field_name, text

    @staticmethod
    def user_tool_results(message: UserMessage) -> list[tuple[str, Any, bool]]:
        """Extract only tool-result blocks from SDK user messages."""
        if not isinstance(message.content, list):
            return []
        return [
            (block.tool_use_id, block.content, bool(block.is_error))
            for block in message.content
            if isinstance(block, ToolResultBlock) and block.tool_use_id
        ]

    @staticmethod
    def block_type(block: ContentBlock) -> str:
        """Return a content-block discriminator for a parsed SDK block."""
        return {
            TextBlock: "text",
            ThinkingBlock: "thinking",
            ToolUseBlock: "tool_use",
            ToolResultBlock: "tool_result",
        }.get(type(block), "")

    @staticmethod
    def content_text(block: ContentBlock) -> str:
        """Read text or thinking content from a parsed SDK block."""
        if isinstance(block, TextBlock):
            return block.text
        return block.thinking if isinstance(block, ThinkingBlock) else ""

    @staticmethod
    def tool_output(value: Any) -> str:
        """Normalize SDK-native tool results into bounded readable text."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("content", "stdout", "output", "result", "message"):
                if key not in value:
                    continue
                nested = value[key]
                if isinstance(nested, list):
                    return "\n".join(
                        str(item.get("text", ""))
                        for item in nested
                        if isinstance(item, dict)
                    )
                if isinstance(nested, (dict, list)):
                    import json

                    return json.dumps(nested, ensure_ascii=False, default=str)
                return str(nested or "")
            import json

            return json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, list):
            return "\n".join(
                str(item.get("text", ""))
                for item in value
                if isinstance(item, dict) and item.get("type") == "text"
            )
        content = getattr(value, "content", None)
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text", ""))
                if isinstance(item, dict)
                else str(getattr(item, "text", ""))
                for item in content
                if (isinstance(item, dict) and item.get("text") is not None)
                or getattr(item, "text", None) is not None
            )
        return str(value)

    @staticmethod
    def result_usage(result: ResultMessage) -> Usage:
        """Map one SDK result's per-run tokens and reported cost to the common type."""
        cost = result.total_cost_usd
        model_usage = result.model_usage
        # ``usage`` covers this run only. ``modelUsage`` is restored by the CLI on
        # ``--resume`` and spans the whole conversation, so summing it would count
        # earlier turns again and inflate the last step_finish (the context size).
        per_run = ClaudeEventNormalizer.message_usage(result.usage)
        if per_run is not None:
            return Usage(
                prompt_tokens=per_run.prompt_tokens,
                completion_tokens=per_run.completion_tokens,
                total_tokens=per_run.total_tokens,
                cost=float(cost or 0.0),
            )
        if isinstance(model_usage, dict) and model_usage:
            input_tokens = sum(
                int(value.get("inputTokens", 0) or 0)
                + int(value.get("cacheReadInputTokens", 0) or 0)
                + int(value.get("cacheCreationInputTokens", 0) or 0)
                for value in model_usage.values()
                if isinstance(value, dict)
            )
            output_tokens = sum(
                int(value.get("outputTokens", 0) or 0)
                for value in model_usage.values()
                if isinstance(value, dict)
            )
        else:
            input_tokens = output_tokens = 0
        return Usage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost=float(cost or 0.0),
        )


__all__ = ["ClaudeEventNormalizer", "StepState"]
