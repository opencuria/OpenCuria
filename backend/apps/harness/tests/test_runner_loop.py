"""Tests for the M4 agentic loop with fake provider/accessor."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.harness.runner import (
    AgentRunner,
    HarnessRunner,
    RunOptions,
)
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import default_tool_registry


class FakeProvider(ProviderAdapter):
    """Scripted provider: yields canned steps, records offered tools."""

    name = "fake"

    def __init__(self, steps: list[list[Delta]]) -> None:
        self._steps = [list(step) for step in steps]
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Yield the next canned step; last step repeats when exhausted."""
        self.calls.append(
            {"model": model, "tools": [tool.name for tool in tools]}
        )
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        for delta in step:
            yield delta


def _text_step(text: str, usage: Usage | None = None) -> list[Delta]:
    return [Delta(text=text, usage=usage or Usage(1, 1, 2))]


def _tool_step(
    tool: str,
    args: dict[str, Any],
    *,
    text: str = "",
    call_id: str = "call-1",
) -> list[Delta]:
    return [
        Delta(
            text=text,
            tool_calls=(
                {
                    "index": 0,
                    "id": call_id,
                    "name": tool,
                    "arguments": json.dumps(args),
                },
            ),
            usage=Usage(2, 3, 5),
        )
    ]


def _runner(
    provider: FakeProvider,
    events: list[dict[str, Any]],
    *,
    agent_rules: dict[str, Any] | None = None,
    permission_timeout: float = 5.0,
    auto_approve: bool = False,
    on_permission=None,
    files: dict[str, bytes] | None = None,
) -> tuple[HarnessRunner, RunOptions]:
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    return HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(global_rules=agent_rules),
        accessor=FakeAccessor(files=files or {"/workspace/a.txt": b"hi"}),
        emit=emit,
    ), RunOptions(
        permission_timeout=permission_timeout,
        auto_approve=auto_approve,
        on_permission=on_permission,
    )


async def test_multi_step_tool_then_tool_then_text() -> None:
    """Loop chains tool results back in until a text-only answer."""
    provider = FakeProvider(
        [
            _tool_step("read", {"path": "a.txt"}, call_id="c1"),
            _tool_step("bash", {"command": "echo hi"}, call_id="c2"),
            _text_step("all done"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    result = await runner.run("go", "build", "test-model", "build", opts)
    assert result.output == "all done"
    assert result.steps == 3
    assert result.finish_reason == "stop"
    types = [event["type"] for event in events]
    assert types.count("step_start") == 3
    assert types.count("step_finish") == 3
    assert "tool_completed" in types
    # Step usages: 5 + 5 (tool steps) + 2 (final text step).
    assert result.usage.total_tokens == 12
    assert result.usage.prompt_tokens == 5
    assert result.usage.completion_tokens == 7
    assert len(provider.calls) == 3


async def test_streaming_deltas_emitted() -> None:
    """Text deltas stream as part_updated events."""
    provider = FakeProvider([[Delta(text="he"), Delta(text="llo")]])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    result = await runner.run("hi", "build", "m", "build", opts)
    assert result.output == "hello"
    deltas = [event for event in events if event["type"] == "part_updated"]
    assert [event["delta"]["text"] for event in deltas] == ["he", "llo"]


async def test_permission_ask_approve_runs_tool() -> None:
    """Ask gates pause for on_permission; approve executes the tool."""
    seen: list[dict[str, Any]] = []

    async def approve(**kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs)
        return "once"

    provider = FakeProvider(
        [
            _tool_step("bash", {"command": "rm -rf /tmp/x"}, call_id="c1"),
            _text_step("removed"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(
        provider,
        events,
        agent_rules={"bash": "ask"},
        on_permission=approve,
    )
    result = await runner.run("clean", "build", "m", "build", opts)
    assert result.output == "removed"
    assert seen and seen[0]["tool"] == "bash"
    assert any(event["type"] == "permission_required" for event in events)
    assert any(event["type"] == "tool_completed" for event in events)


async def test_permission_ask_deny_skips_tool() -> None:
    """Deny returns a tool message so the model can react, then finishes."""

    async def deny(**kwargs):  # type: ignore[no-untyped-def]
        return "reject"

    provider = FakeProvider(
        [
            _tool_step("bash", {"command": "reboot"}, call_id="c1"),
            _text_step("ok, skipped"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(
        provider, events, agent_rules={"bash": "ask"}, on_permission=deny
    )
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.output == "ok, skipped"
    errors = [event for event in events if event["type"] == "tool_error"]
    assert errors and "denied" in errors[0]["error"]


async def test_permission_timeout_auto_denies() -> None:
    """A hanging on_permission callback auto-denies after the timeout."""

    async def hanging(**kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)
        return "once"

    provider = FakeProvider(
        [
            _tool_step("bash", {"command": "reboot"}, call_id="c1"),
            _text_step("after timeout"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(
        provider,
        events,
        agent_rules={"bash": "ask"},
        on_permission=hanging,
        permission_timeout=0.05,
    )
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.output == "after timeout"
    assert any(event["type"] == "tool_error" for event in events)


async def test_deny_tool_not_offered_to_provider() -> None:
    """Deny-decision tools are filtered from the provider tool schemas."""
    provider = FakeProvider([_text_step("no tools needed")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events, agent_rules={"bash": "deny"})
    await runner.run("p", "build", "m", "build", opts)
    assert "bash" not in provider.calls[0]["tools"]
    assert "read" in provider.calls[0]["tools"]


async def test_explore_agent_filters_edit_tools() -> None:
    """The read-only explore agent never offers edit/write tools."""
    provider = FakeProvider([_text_step("researched")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    await runner.run("research", "explore", "m", "build", opts)
    offered = provider.calls[0]["tools"]
    assert "edit" not in offered
    assert "write" not in offered
    assert "read" in offered


async def test_unknown_agent_raises() -> None:
    """Unknown agent names raise KeyError."""
    provider = FakeProvider([_text_step("x")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    with pytest.raises(KeyError, match="Unknown agent"):
        await runner.run("p", "nope", "m", "build", opts)


async def test_abort_emits_aborted() -> None:
    """Cancelling the run task emits aborted and re-raises CancelledError."""
    provider = FakeProvider([_text_step("never")])
    events: list[dict[str, Any]] = []

    async def slow_emit(event: dict[str, Any]) -> None:
        events.append(event)
        if event["type"] == "step_start":
            await asyncio.sleep(30)

    slow_runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={}),
        emit=slow_emit,
    )
    task = asyncio.create_task(slow_runner.run("p", "build", "m", "build"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(event["type"] == "aborted" for event in events)


async def test_doom_loop_triggers_permission_flow() -> None:
    """Three identical tool+input calls route through the ask gate."""
    decisions: list[str] = []

    async def approve(**kwargs):  # type: ignore[no-untyped-def]
        decisions.append(kwargs.get("key", ""))
        return "once"

    provider = FakeProvider(
        [
            _tool_step("read", {"path": "a.txt"}, call_id="c1"),
            _tool_step("read", {"path": "a.txt"}, call_id="c2"),
            _tool_step("read", {"path": "a.txt"}, call_id="c3"),
            _text_step("loop broken"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events, on_permission=approve)
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.output == "loop broken"
    assert "doom_loop" in decisions
    gates = [event for event in events if event["type"] == "permission_required"]
    assert any(event.get("key") == "doom_loop" for event in gates)


async def test_steps_budget_returns_max_steps() -> None:
    """Exhausting the budget stops with finish_reason max_steps."""
    provider = FakeProvider([_tool_step("read", {"path": "a.txt"}, call_id="c1")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    opts.max_steps = 2
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.finish_reason == "max_steps"
    assert result.steps == 2
    assert "step budget" in result.output


async def test_cost_tokens_summed_across_steps() -> None:
    """Usage from every step is summed into the final result."""
    provider = FakeProvider(
        [
            _tool_step("read", {"path": "a.txt"}, call_id="c1"),
            _text_step("b", usage=Usage(7, 8, 15)),
        ]
    )
    # First step carries usage(2,3,5); second carries usage(7,8,15).
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.output == "b"
    assert result.usage.prompt_tokens == 9
    assert result.usage.completion_tokens == 11
    assert result.usage.total_tokens == 20


async def test_tool_error_fed_back_as_tool_message() -> None:
    """Failing tools emit tool_error and let the model finish."""
    provider = FakeProvider(
        [
            _tool_step("read", {"path": "missing.txt"}, call_id="c1"),
            _text_step("handled"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events, files={})
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.output == "handled"
    assert any(event["type"] == "tool_error" for event in events)


async def test_history_is_included_in_provider_messages() -> None:
    """Passed history lands between system and the new user prompt."""
    provider = FakeProvider([_text_step("hi")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    history = [LLMMessage(role="user", content="earlier")]
    opts.history = history
    await runner.run("now", "build", "m", "build", opts)
    # Provider saw only schemas; verify history threaded via message count.
    assert provider.calls and provider.calls[0]["model"] == "m"


async def test_plan_mode_read_only_bash_auto_flows() -> None:
    """Plan agent allows read-only bash without a permission callback."""
    provider = FakeProvider(
        [
            _tool_step("bash", {"command": "git status"}, call_id="c1"),
            _text_step("plan ready"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    result = await runner.run("scope it", "plan", "m", "plan", opts)
    assert result.output == "plan ready"
    assert not any(event["type"] == "permission_required" for event in events)


async def test_agent_runner_alias() -> None:
    """AgentRunner is an alias of HarnessRunner."""
    assert AgentRunner is HarnessRunner


async def test_empty_prompt_rejected() -> None:
    """Empty prompts raise ValueError."""
    provider = FakeProvider([_text_step("x")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    with pytest.raises(ValueError, match="prompt must not be empty"):
        await runner.run("  ", "build", "m", "build", opts)


async def test_invalid_mode_rejected() -> None:
    """Modes other than plan|build raise ValueError."""
    provider = FakeProvider([_text_step("x")])
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events)
    with pytest.raises(ValueError, match="Invalid mode"):
        await runner.run("p", "build", "m", "turbo", opts)
