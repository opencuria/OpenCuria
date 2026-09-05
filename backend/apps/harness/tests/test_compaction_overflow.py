"""Overflow math and compaction trigger helpers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.compaction import (
    CHECKPOINT_PREFIX,
    COMPACTION_BUFFER,
    OUTPUT_TOKEN_MAX,
    ModelLimits,
    apply_compaction,
    build_compaction_prompt,
    find_previous_summary,
    is_context_overflow_error,
    is_overflow,
    max_output_tokens,
    preserve_recent_budget,
    select,
    serialize,
    token_count,
    usable,
)
from apps.harness.providers.base import Delta, LLMMessage, ProviderAdapter, ToolSchema, Usage
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tests.test_paket6_features import ScriptProvider
from apps.harness.tools import default_tool_registry


def test_max_output_tokens_caps_and_defaults() -> None:
    """Resolved max output follows OpenCode min-or-default rule."""
    assert max_output_tokens(ModelLimits(max_output_tokens=16_384)) == 16_384
    assert max_output_tokens(ModelLimits(max_output_tokens=64_000)) == OUTPUT_TOKEN_MAX
    assert max_output_tokens(ModelLimits(max_output_tokens=0)) == OUTPUT_TOKEN_MAX


def test_usable_openrouter_branch() -> None:
    """Usable input budget is context minus capped max output."""
    limits = ModelLimits(context_length=200_000, max_output_tokens=16_384)
    assert usable(limits) == 200_000 - 16_384
    assert usable(ModelLimits(context_length=0, max_output_tokens=16_384)) == 0


def test_is_overflow_respects_context_and_auto() -> None:
    """Overflow triggers on last-step count vs usable, not session totals."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=1_000)
    budget = usable(limits)
    assert is_overflow(
        prompt_tokens=budget,
        completion_tokens=0,
        limits=limits,
    )
    assert not is_overflow(
        prompt_tokens=budget - 1,
        completion_tokens=0,
        limits=limits,
    )
    assert not is_overflow(
        prompt_tokens=budget,
        completion_tokens=0,
        limits=limits,
        auto=False,
    )


def test_context_length_zero_never_overflows() -> None:
    """Unknown/zero context disables auto-compaction."""
    limits = ModelLimits(context_length=0, max_output_tokens=8_192)
    assert not is_overflow(
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        limits=limits,
    )


def test_token_count_prefers_total() -> None:
    """Count uses total when present, otherwise prompt plus completion."""
    assert token_count(100, 50, total=0) == 150
    assert token_count(100, 50, total=999) == 999


def test_preserve_recent_budget_formula() -> None:
    """Preserve budget clamps between min/max and scales with usable."""
    limits = ModelLimits(context_length=200_000, max_output_tokens=16_384)
    expected = min(15_000, max(2_000, int(usable(limits) * 0.25)))
    assert preserve_recent_budget(limits) == expected

    tiny = ModelLimits(context_length=5_000, max_output_tokens=1_000)
    assert preserve_recent_budget(tiny) == 2_000


def test_is_context_overflow_error_matches_phrases() -> None:
    """Provider context errors are detected by message substring."""
    assert is_context_overflow_error(RuntimeError("maximum context length exceeded"))
    assert is_context_overflow_error(ValueError("prompt is too long: 999 > 100"))
    assert not is_context_overflow_error(RuntimeError("rate limit exceeded"))


def test_serialize_replaces_images_with_placeholder() -> None:
    """Image parts become ``[image]`` in compaction serialization."""
    message = LLMMessage(
        role="user",
        content=[
            {"type": "text", "text": "see "},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    )
    text = serialize(message)
    assert "AAAA" not in text
    assert "[image]" in text


def test_build_compaction_prompt_includes_template() -> None:
    """Compaction prompt uses the structured summary template."""
    prompt = build_compaction_prompt(
        [LLMMessage(role="user", content="hello")],
    )
    assert "## Objective" in prompt
    assert "<conversation>" in prompt


def test_checkpoint_summary_round_trip() -> None:
    """Prior checkpoint summaries feed the next compaction prompt."""
    messages = [
        LLMMessage(
            role="user",
            content=f"{CHECKPOINT_PREFIX}\n## Objective\n- old goal",
        ),
        LLMMessage(role="user", content="newer", message_id="u2"),
    ]
    assert find_previous_summary(messages) == "## Objective\n- old goal"
    prompt = build_compaction_prompt(
        [LLMMessage(role="user", content="new turn")],
        previous_summary=find_previous_summary(messages),
    )
    assert "<prior-summary>" in prompt


def test_select_skips_checkpoint_user_turns() -> None:
    """Checkpoint messages are not treated as real user turns."""
    limits = ModelLimits(context_length=200_000, max_output_tokens=16_384)
    messages = [
        LLMMessage(
            role="user",
            content=f"{CHECKPOINT_PREFIX}\nold summary",
        ),
        LLMMessage(role="user", content="real old", message_id="old"),
        LLMMessage(role="assistant", content="answer"),
        LLMMessage(role="user", content="current", message_id="current"),
    ]
    selection = select(messages, limits)
    assert selection.tail[-1].content == "current"
    assert selection.tail_start_id == "old"


def test_compaction_buffer_constant() -> None:
    """Compaction buffer matches OpenCode headroom hint."""
    assert COMPACTION_BUFFER == 20_000


def test_select_keeps_current_user_when_all_turns_fit_budget() -> None:
    """When every turn fits the preserve budget, the current user stays in tail."""
    limits = ModelLimits(context_length=200_000, max_output_tokens=16_384)
    messages = [
        LLMMessage(role="user", content="small", message_id="u1"),
        LLMMessage(role="assistant", content="answer"),
        LLMMessage(role="user", content="current", message_id="current"),
    ]
    selection = select(messages, limits)
    assert selection.tail[-1].message_id == "current"
    assert selection.tail_start_id == "current"


def test_select_single_user_turn_has_empty_head() -> None:
    """A lone user turn must never move into the summarizable head."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [LLMMessage(role="user", content="only", message_id="only")]
    selection = select(messages, limits)
    assert selection.head == []
    assert selection.tail[0].message_id == "only"


def test_select_keeps_huge_current_user_in_tail() -> None:
    """The latest user turn stays in the tail even when it exceeds budget."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [
        LLMMessage(role="user", content="old", message_id="old"),
        LLMMessage(role="assistant", content="answer"),
        LLMMessage(role="user", content="x" * 50_000, message_id="current"),
    ]
    selection = select(messages, limits)
    assert selection.head
    assert selection.tail[-1].message_id == "current"


def test_apply_compaction_noop_for_single_user_turn() -> None:
    """apply_compaction does not drop the only user message."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [LLMMessage(role="user", content="only", message_id="only")]
    compacted, tail_start_id = apply_compaction(
        messages,
        "## Objective\n- summary",
        limits,
    )
    assert compacted == messages
    assert tail_start_id == "only"


def test_apply_compaction_keeps_current_user_when_all_turns_fit_budget() -> None:
    """Small multi-turn conversations keep the latest user message in the tail."""
    limits = ModelLimits(context_length=200_000, max_output_tokens=16_384)
    messages = [
        LLMMessage(role="user", content="a", message_id="u1"),
        LLMMessage(role="assistant", content="b"),
        LLMMessage(role="user", content="current", message_id="current"),
    ]
    compacted, tail_start_id = apply_compaction(
        messages,
        "## Objective\n- summary",
        limits,
    )
    assert compacted[-1].content == "current"
    assert compacted[-1].message_id == "current"
    assert tail_start_id == "current"
    assert not any(message.content == "a" for message in compacted if message.role == "user")


class OverflowRetryProvider(ProviderAdapter):
    """Fail once with a context error, then serve compaction and retry."""

    name = "overflow-retry"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts=None,
    ) -> AsyncIterator[Delta]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("maximum context length exceeded")
        if self.calls == 2:
            yield Delta(text="## Objective\n- recovered", usage=Usage(1, 1, 2))
            return
        yield Delta(text="final answer", usage=Usage(1, 1, 2))


def _overflow_usage_step(text: str) -> list[Delta]:
    """Return a text step whose usage exceeds a 1k/100 limits pair."""
    return [Delta(text=text, usage=Usage(500, 500, 1_000))]


@pytest.mark.asyncio
async def test_provider_context_overflow_error_compacts_and_retries() -> None:
    """Provider context errors compact with overflow=True and retry once."""
    provider = OverflowRetryProvider()
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        emit=emit,
    )
    result = await runner.run(
        "follow-up",
        "build",
        "session-model",
        "build",
        RunOptions(
            auto_approve=True,
            context_length=1_000,
            max_output_tokens=100,
            history=[
                LLMMessage(role="user", content="old"),
                LLMMessage(role="assistant", content="prior answer"),
            ],
        ),
    )
    assert result.output == "final answer"
    assert provider.calls == 3
    compaction_events = [event for event in events if event.get("type") == "compaction"]
    assert compaction_events
    assert compaction_events[0].get("overflow") is True


@pytest.mark.asyncio
async def test_text_only_overflow_compacts_without_second_answer() -> None:
    """Text-only overflow compacts once and returns the answer (two provider calls)."""
    provider = ScriptProvider(
        [
            _overflow_usage_step("done"),
            [Delta(text="## Objective\n- summary", usage=Usage(1, 1, 2))],
        ]
    )
    runner = HarnessRunner(provider=provider, tools=default_tool_registry())
    result = await runner.run(
        "follow-up",
        "build",
        "session-model",
        "build",
        RunOptions(
            auto_approve=True,
            context_length=1_000,
            max_output_tokens=100,
            history=[
                LLMMessage(role="user", content="old"),
                LLMMessage(role="assistant", content="prior answer"),
            ],
        ),
    )
    assert result.output == "done"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_tool_step_overflow_does_not_compact_twice_same_turn() -> None:
    """Prior-turn compact plus post-tool compact; no duplicate on step two."""
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    provider = ScriptProvider(
        [
            [Delta(text="## Objective\n- first", usage=Usage(1, 1, 2))],
            [
                Delta(
                    tool_calls=(
                        {
                            "index": 0,
                            "id": "call-1",
                            "name": "read",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    ),
                    usage=Usage(500, 500, 1_000),
                )
            ],
            [Delta(text="## Objective\n- second", usage=Usage(1, 1, 2))],
            [Delta(text="final", usage=Usage(1, 1, 2))],
        ]
    )
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={"/workspace/README.md": b"hello"}),
        emit=emit,
    )
    result = await runner.run(
        "follow-up",
        "build",
        "session-model",
        "build",
        RunOptions(
            auto_approve=True,
            context_length=1_000,
            max_output_tokens=100,
            last_step_prompt_tokens=500,
            last_step_completion_tokens=500,
            history=[
                LLMMessage(role="user", content="old"),
                LLMMessage(role="assistant", content="prior answer"),
            ],
        ),
    )
    assert result.output == "final"
    compaction_events = [event for event in events if event.get("type") == "compaction"]
    assert len(compaction_events) == 2
    assert all(event.get("overflow") is False for event in compaction_events)


@pytest.mark.asyncio
async def test_compaction_never_adds_synthetic_continue_user_message() -> None:
    """Compacted history must not inject a synthetic continue prompt."""
    seen_user_texts: list[str] = []

    class CapturingProvider(ScriptProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            for message in messages:
                if message.role == "user" and isinstance(message.content, str):
                    seen_user_texts.append(message.content)
            async for delta in super().chat_stream(model, messages, tools, opts):
                yield delta

    provider = CapturingProvider(
        [
            [Delta(text="## Objective\n- summary", usage=Usage(1, 1, 2))],
            [Delta(text="final", usage=Usage(1, 1, 2))],
        ]
    )
    runner = HarnessRunner(provider=provider, tools=default_tool_registry())
    await runner.run(
        "follow-up",
        "build",
        "session-model",
        "build",
        RunOptions(
            auto_approve=True,
            context_length=1_000,
            max_output_tokens=100,
            last_step_prompt_tokens=500,
            last_step_completion_tokens=500,
            history=[
                LLMMessage(role="user", content="old"),
                LLMMessage(role="assistant", content="prior answer"),
            ],
        ),
    )
    assert seen_user_texts
    assert not any("Continue if you have next steps" in text for text in seen_user_texts)
