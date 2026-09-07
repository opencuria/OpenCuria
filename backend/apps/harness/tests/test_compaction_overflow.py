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
    apply_overflow_replay,
    build_compaction_prompt,
    ensure_current_user,
    find_previous_summary,
    is_context_overflow_error,
    is_overflow,
    max_output_tokens,
    preserve_recent_budget,
    select,
    serialize,
    strip_media,
    token_count,
    usable,
)
from apps.harness.providers.base import (
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
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
    ]
    for message in negatives:
        assert not is_context_overflow_error(RuntimeError(message)), message


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


def test_select_single_user_turn_summarizes_all() -> None:
    """A lone user turn has no retained tail (OpenCode keep.start === 0)."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [LLMMessage(role="user", content="only", message_id="only")]
    selection = select(messages, limits)
    assert selection.head == messages
    assert selection.tail == []
    assert selection.tail_start_id == ""


def test_select_huge_current_user_is_head_not_tail() -> None:
    """An oversized latest turn is summarized instead of forced into the tail."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [
        LLMMessage(role="user", content="old", message_id="old"),
        LLMMessage(role="assistant", content="answer"),
        LLMMessage(role="user", content="x" * 50_000, message_id="current"),
    ]
    selection = select(messages, limits)
    assert selection.head
    assert selection.tail == []
    assert any(message.message_id == "current" for message in selection.head)


def test_select_all_turns_fitting_budget_has_no_tail() -> None:
    """When every turn fits, OpenCode keeps no tail (summarize everything)."""
    limits = ModelLimits(context_length=200_000, max_output_tokens=16_384)
    messages = [
        LLMMessage(role="user", content="small", message_id="u1"),
        LLMMessage(role="assistant", content="answer"),
        LLMMessage(role="user", content="current", message_id="current"),
    ]
    selection = select(messages, limits)
    assert selection.tail == []
    assert selection.head[-1].message_id == "current"


def test_select_splits_oversized_turn_after_user() -> None:
    """splitTurn may retain a suffix that starts after the user message."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [
        LLMMessage(role="user", content="old", message_id="old"),
        LLMMessage(role="assistant", content="answer"),
        LLMMessage(role="user", content="now", message_id="current"),
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[{"id": "c1", "name": "grep", "arguments": "{}"}],
        ),
        LLMMessage(role="tool", content="x" * 50_000, tool_call_id="c1"),
        LLMMessage(role="assistant", content="tiny"),
    ]
    selection = select(messages, limits)
    assert selection.head
    if selection.tail:
        assert (
            selection.tail[0].role != "user"
            or selection.tail[0].message_id != "current"
        )
        assert all(
            not (message.role == "tool" and len(str(message.content or "")) > 10_000)
            for message in selection.tail
        )


def test_apply_compaction_replaces_single_turn_with_checkpoint() -> None:
    """apply_compaction summarizes a lone user turn; runner re-inserts the prompt."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [LLMMessage(role="user", content="only", message_id="only")]
    compacted, tail_start_id = apply_compaction(
        messages,
        "## Objective\n- summary",
        limits,
    )
    assert tail_start_id == ""
    assert compacted[0].content.startswith(CHECKPOINT_PREFIX)
    restored = ensure_current_user(messages, compacted)
    assert restored[-1].content == "only"


def test_apply_compaction_keeps_current_user_when_older_turn_is_head() -> None:
    """When the latest turn fits and older turns do not, the current user stays."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [
        LLMMessage(role="user", content="a" * 8_000, message_id="u1"),
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
    assert not any(
        message.content == "a" * 8_000
        for message in compacted
        if message.role == "user"
    )


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


def test_strip_media_replaces_image_parts() -> None:
    """Overflow replay must not send base64 image payloads."""
    message = LLMMessage(
        role="user",
        content=[
            {"type": "text", "text": "see "},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        message_id="u1",
    )
    stripped = strip_media(message)
    assert isinstance(stripped.content, str)
    assert "AAAA" not in stripped.content
    assert "[Attached image: file]" in stripped.content
    assert "see " in stripped.content


def test_apply_overflow_replay_drops_tool_results() -> None:
    """Overflow replay keeps the user prompt and drops overflowing tools."""
    original = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="old", message_id="old"),
        LLMMessage(role="assistant", content="prior"),
        LLMMessage(role="user", content="follow-up", message_id="current"),
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[{"id": "c1", "name": "grep", "arguments": "{}"}],
        ),
        LLMMessage(role="tool", content="x" * 10_000, tool_call_id="c1"),
    ]
    compacted = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(
            role="user",
            content=f"{CHECKPOINT_PREFIX}\n## Objective\n- recovered",
        ),
        LLMMessage(role="tool", content="x" * 10_000, tool_call_id="c1"),
    ]
    replayed = apply_overflow_replay(original, compacted)
    assert replayed[0].role == "system"
    assert replayed[-1].content == "follow-up"
    assert not any(message.role == "tool" for message in replayed)


class OverflowRetryCaptureProvider(ProviderAdapter):
    """Overflow once, compact, then capture the retry payload."""

    name = "overflow-retry-capture"

    def __init__(self) -> None:
        self.calls = 0
        self.retry_messages: list[LLMMessage] = []

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
        self.retry_messages = list(messages)
        yield Delta(text="final answer", usage=Usage(1, 1, 2))


class AlwaysOverflowAfterCompactProvider(ProviderAdapter):
    """Overflow, compact successfully, then overflow again on retry."""

    name = "always-overflow"

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
        if self.calls == 2:
            yield Delta(text="## Objective\n- summary", usage=Usage(1, 1, 2))
            return
        raise RuntimeError("maximum context length exceeded")


@pytest.mark.asyncio
async def test_overflow_retry_drops_huge_tool_output() -> None:
    """Provider overflow retries without the overflowing tool blob."""
    provider = OverflowRetryCaptureProvider()
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
                LLMMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[{"id": "c1", "name": "grep", "arguments": "{}"}],
                ),
                LLMMessage(role="tool", content="Force" * 20_000, tool_call_id="c1"),
            ],
        ),
    )
    assert result.output == "final answer"
    joined = json.dumps(
        [
            message.content
            for message in provider.retry_messages
            if isinstance(message.content, str)
        ]
    )
    assert "Force" * 50 not in joined
    assert any(
        message.role == "user" and message.content == "follow-up"
        for message in provider.retry_messages
    )


@pytest.mark.asyncio
async def test_overflow_after_compaction_stops_without_loop() -> None:
    """A second context overflow after compaction fails the run once."""
    provider = AlwaysOverflowAfterCompactProvider()
    runner = HarnessRunner(provider=provider, tools=default_tool_registry())
    with pytest.raises(RuntimeError, match="too large to compact"):
        await runner.run(
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
    assert provider.calls == 3

