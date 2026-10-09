"""Claude SDK message parsing and normalized stream-event helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple

from ...providers.base import Usage


@dataclass
class StepState:
    """Streaming state for one root turn or a delegated CLI subagent."""

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
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)


class SdkMessageTypes(NamedTuple):
    """SDK message classes used for result and task lifecycle dispatch."""

    assistant: type
    result: type
    stream: type
    user: type
    system: type
    task_notification: type
    task_progress: type
    task_started: type


class ClaudeEventNormalizer:
    """Stateless conversion from Claude SDK payloads to harness primitives."""

    @staticmethod
    def message_types() -> SdkMessageTypes:
        """Load the pinned SDK's message classes lazily."""
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ResultMessage,
                StreamEvent,
                SystemMessage,
                TaskNotificationMessage,
                TaskProgressMessage,
                TaskStartedMessage,
                UserMessage,
            )
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc
        return SdkMessageTypes(
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            UserMessage,
            SystemMessage,
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
        )

    @staticmethod
    def stream_event(message: Any) -> dict[str, Any]:
        """Return a raw SDK stream event from a parsed message or fixture."""
        raw = message.get("event") if isinstance(message, dict) else getattr(message, "event", None)
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def stream_parent(message: Any) -> str:
        """Return the root/child scope attached to an SDK stream event."""
        parent = (
            message.get("parent_tool_use_id")
            if isinstance(message, dict)
            else getattr(message, "parent_tool_use_id", None)
        )
        return str(parent or "")

    @classmethod
    def stream_message_id(cls, message: Any) -> str:
        """Read the Anthropic API message ID from ``message_start``."""
        raw = cls.stream_event(message)
        api_message = raw.get("message")
        return str(api_message.get("id") or "") if isinstance(api_message, dict) else ""

    @staticmethod
    def stream_fallback_id(message: Any) -> str:
        """Return a fixture-only fallback when API message_start is absent."""
        value = (
            message.get("uuid")
            if isinstance(message, dict)
            else getattr(message, "uuid", None)
        )
        return str(value or "")

    @classmethod
    def stream_block_start(cls, message: Any) -> tuple[int, str, str, str] | None:
        """Return content-block index, type, tool name and tool-use ID."""
        raw = cls.stream_event(message)
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

    @classmethod
    def stream_block_stop(cls, message: Any) -> int | None:
        """Return the index of a completed content block, if present."""
        raw = cls.stream_event(message)
        if raw.get("type") != "content_block_stop":
            return None
        try:
            return int(raw.get("index", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def assistant_message_id(message: Any) -> str:
        """Read the Anthropic API message ID from an assistant completion."""
        if isinstance(message, dict):
            raw_message = message.get("message")
            message_id = raw_message.get("id") if isinstance(raw_message, dict) else None
            return str(message_id or message.get("uuid") or "")
        return str(
            getattr(message, "message_id", None)
            or getattr(message, "uuid", None)
            or ""
        )

    @classmethod
    def partial_delta(cls, message: Any) -> tuple[str, int, str, str, str, str] | None:
        """Return parent/index/slot/field/text and fallback ID for a delta."""
        raw = cls.stream_event(message)
        if raw.get("type") != "content_block_delta":
            return None
        delta = raw.get("delta")
        if not isinstance(delta, dict):
            return None
        kind = str(delta.get("type") or "")
        if kind == "text_delta":
            text, slot, field_name = str(delta.get("text") or ""), "text", "text"
        elif kind == "thinking_delta":
            text, slot, field_name = str(delta.get("thinking") or ""), "thinking", "reasoning"
        else:
            return None
        try:
            index = int(raw.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        return (
            cls.stream_parent(message),
            index,
            slot,
            field_name,
            text,
            cls.stream_fallback_id(message),
        )

    @staticmethod
    def assistant_blocks(message: Any) -> tuple[str, list[Any]]:
        """Return parent tool-use ID and assistant content blocks."""
        parent = getattr(message, "parent_tool_use_id", None)
        blocks = getattr(message, "content", None)
        if isinstance(message, dict):
            parent = message.get("parent_tool_use_id")
            raw_message = message.get("message")
            blocks = raw_message.get("content", message.get("content")) if isinstance(raw_message, dict) else message.get("content")
        return str(parent or ""), blocks if isinstance(blocks, list) else []

    @staticmethod
    def user_tool_results(message: Any) -> list[tuple[str, Any, bool]]:
        """Extract only tool-result blocks from SDK user messages."""
        content = getattr(message, "content", None)
        if isinstance(message, dict):
            raw = message.get("message")
            content = raw.get("content") if isinstance(raw, dict) else message.get("content")
        if not isinstance(content, list):
            return []
        results: list[tuple[str, Any, bool]] = []
        for block in content:
            if ClaudeEventNormalizer.block_type(block) != "tool_result":
                continue
            if isinstance(block, dict):
                call_id, value, error = block.get("tool_use_id"), block.get("content"), block.get("is_error")
            else:
                call_id = getattr(block, "tool_use_id", None)
                value = getattr(block, "content", None)
                error = getattr(block, "is_error", None)
            if call_id:
                results.append((str(call_id), value, bool(error)))
        return results

    @staticmethod
    def block_type(block: Any) -> str:
        """Return a content-block discriminator for SDK objects and fixtures."""
        if isinstance(block, dict):
            return str(block.get("type") or "")
        return {
            "TextBlock": "text", "ThinkingBlock": "thinking",
            "ToolUseBlock": "tool_use", "ToolResultBlock": "tool_result",
        }.get(type(block).__name__, "")

    @staticmethod
    def content_text(block: Any) -> str:
        """Read text/thinking content from an SDK block or raw fixture."""
        if isinstance(block, dict):
            return str(block.get("text") or block.get("thinking") or "")
        return str(getattr(block, "text", None) or getattr(block, "thinking", None) or "")

    @classmethod
    def assistant_deltas(
        cls, message: Any, streamed: dict[tuple[int, str], str]
    ) -> tuple[str, list[tuple[int, str, str, str]]]:
        """Return final content not already emitted by partial deltas."""
        parent, blocks = cls.assistant_blocks(message)
        deltas: list[tuple[int, str, str, str]] = []
        for index, block in enumerate(blocks):
            kind = cls.block_type(block)
            if kind not in {"text", "thinking"}:
                continue
            full = cls.content_text(block)
            if not full:
                continue
            slot = "text" if kind == "text" else "thinking"
            field_name = "text" if kind == "text" else "reasoning"
            previous = streamed.get((index, slot), "")
            remainder = full[len(previous):] if previous and full.startswith(previous) else full
            if remainder:
                deltas.append((index, slot, field_name, remainder))
        return parent, deltas

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
                    return "\n".join(str(item.get("text", "")) for item in nested if isinstance(item, dict))
                if isinstance(nested, (dict, list)):
                    import json
                    return json.dumps(nested, ensure_ascii=False, default=str)
                return str(nested or "")
            import json
            return json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, list):
            return "\n".join(str(item.get("text", "")) for item in value if isinstance(item, dict) and item.get("type") == "text")
        content = getattr(value, "content", None)
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(getattr(item, "text", ""))
                for item in content
                if (isinstance(item, dict) and item.get("text") is not None)
                or getattr(item, "text", None) is not None
            )
        return str(value)

    @staticmethod
    def result_usage(result: Any) -> Usage:
        """Map one SDK result's cumulative usage and cost to the common type."""
        if isinstance(result, dict):
            raw = result.get("usage")
            cost = result.get("total_cost_usd", result.get("totalCostUSD", 0.0))
            model_usage = result.get("model_usage", result.get("modelUsage"))
        else:
            raw = getattr(result, "usage", None)
            cost = getattr(result, "total_cost_usd", 0.0)
            model_usage = getattr(result, "model_usage", None)
        if not isinstance(model_usage, dict):
            model_usage = getattr(result, "modelUsage", None)
        raw = raw if isinstance(raw, dict) else {}
        if isinstance(model_usage, dict) and model_usage:
            input_tokens = sum(
                int(value.get("inputTokens", 0) or 0)
                + int(value.get("cacheReadInputTokens", 0) or 0)
                + int(value.get("cacheCreationInputTokens", 0) or 0)
                for value in model_usage.values() if isinstance(value, dict)
            )
            output_tokens = sum(
                int(value.get("outputTokens", 0) or 0)
                for value in model_usage.values() if isinstance(value, dict)
            )
        else:
            input_tokens = int(raw.get("input_tokens", raw.get("inputTokens", 0)) or 0)
            output_tokens = int(raw.get("output_tokens", raw.get("outputTokens", 0)) or 0)
        return Usage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost=float(cost or 0.0),
        )


__all__ = ["ClaudeEventNormalizer", "SdkMessageTypes", "StepState"]
