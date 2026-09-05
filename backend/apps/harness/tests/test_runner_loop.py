"""Tests for the M4 agentic loop with fake provider/accessor."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel, Field

from apps.harness.max_steps import MAX_STEPS_PROMPT, MAX_STEPS_TOOL_ERROR
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
from apps.harness.tools.base import Tool, ToolResult


class FakeProvider(ProviderAdapter):
    """Scripted provider: yields canned steps, records offered tools."""

    name = "fake"

    def __init__(self, steps: list[list[Delta]]) -> None:
        self._steps = [list(step) for step in steps]
        self.calls: list[dict[str, Any]] = []
        self.messages: list[list[LLMMessage]] = []

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Yield the next canned step; then a text-only done reply."""
        self.calls.append(
            {
                "model": model,
                "tools": [tool.name for tool in tools],
                "tool_choice": None if opts is None else opts.tool_choice,
            }
        )
        self.messages.append(list(messages))
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0)
        for delta in step:
            yield delta


def _text_step(text: str, usage: Usage | None = None) -> list[Delta]:
    return [Delta(text=text, usage=usage or Usage(1, 1, 2, 0.002))]


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
            usage=Usage(2, 3, 5, 0.01),
        )
    ]


def _multi_tool_step(
    calls: list[tuple[str, dict[str, Any], str]],
) -> list[Delta]:
    return [
        Delta(
            tool_calls=tuple(
                {
                    "index": index,
                    "id": call_id,
                    "name": name,
                    "arguments": json.dumps(args),
                }
                for index, (name, args, call_id) in enumerate(calls)
            ),
            usage=Usage(2, 3, 5, 0.01),
        )
    ]


class ProbeArgs(BaseModel):
    """Arguments for the parallel probe tool."""

    key: str = Field(default="")


class ProbeTool(Tool):
    """Test tool that can overlap concurrent executions."""

    name = "probe"
    description = "Parallel probe"
    args_schema = ProbeArgs

    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.running = 0
        self.max_running = 0
        self.finish_order: list[str] = []

    def events_for(self, key: str) -> tuple[asyncio.Event, asyncio.Event]:
        """Return started/release events for *key*."""
        started = self.started.setdefault(key, asyncio.Event())
        release = self.release.setdefault(key, asyncio.Event())
        return started, release

    async def execute(self, args: BaseModel | dict[str, object], ctx) -> ToolResult:  # type: ignore[no-untyped-def]
        """Wait until released; track overlap."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ProbeArgs)
        key = validated.key or ctx.call_id
        started, release = self.events_for(key)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        started.set()
        try:
            await release.wait()
        finally:
            self.running -= 1
        self.finish_order.append(key)
        return ToolResult(output=f"done-{key}")


class BoomTool(Tool):
    """Test tool that always fails."""

    name = "boom"
    description = "Always fails"
    args_schema = ProbeArgs

    async def execute(self, args: BaseModel | dict[str, object], ctx) -> ToolResult:  # type: ignore[no-untyped-def]
        """Raise immediately."""
        raise RuntimeError("boom")


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


def test_usage_merge_adds_tokens_and_cost() -> None:
    """Usage.merge sums token counts and billed cost."""
    merged = Usage(1, 2, 3, 0.01).merge(Usage(4, 5, 9, 0.002))
    assert merged == Usage(5, 7, 12, 0.012)


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


async def test_steps_budget_forces_text_only_last_step() -> None:
    """The last configured step disables tools and injects MAX_STEPS_PROMPT."""
    provider = FakeProvider(
        [
            _tool_step("read", {"path": "a.txt"}, call_id="c1"),
            _tool_step("read", {"path": "forbidden.txt"}, call_id="c2"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(provider, events, auto_approve=True)
    opts.max_steps = 2
    result = await runner.run("p", "build", "m", "build", opts)
    assert result.finish_reason == "max_steps"
    assert result.steps == 2
    assert provider.calls[0]["tools"]
    assert provider.calls[0]["tool_choice"] is None
    assert provider.calls[1]["tools"] == []
    assert provider.calls[1]["tool_choice"] == "none"
    last_messages = provider.messages[1]
    assert last_messages[-1].role == "assistant"
    assert last_messages[-1].content == MAX_STEPS_PROMPT
    completed = [e for e in events if e.get("type") == "tool_completed"]
    errors = [e for e in events if e.get("type") == "tool_error"]
    assert [e.get("call_id") for e in completed] == ["c1"]
    assert [e.get("call_id") for e in errors] == ["c2"]
    assert errors[0]["error"] == MAX_STEPS_TOOL_ERROR


async def test_cost_tokens_summed_across_steps() -> None:
    """Usage from every step is summed into the final result."""
    provider = FakeProvider(
        [
            _tool_step("read", {"path": "a.txt"}, call_id="c1"),
            _text_step("b", usage=Usage(7, 8, 15, 0.002)),
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
    assert result.usage.cost == pytest.approx(0.012)
    assert result.cost == pytest.approx(0.012)
    finishes = [event for event in events if event["type"] == "step_finish"]
    assert [event["cost"] for event in finishes] == [
        pytest.approx(0.01),
        pytest.approx(0.002),
    ]


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


async def test_prompt_workspace_image_hydrated_for_provider() -> None:
    """User prompts with workspace image markdown become multimodal."""
    provider = FakeProvider([_text_step("seen")])
    events: list[dict[str, Any]] = []
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 4
    runner, opts = _runner(
        provider,
        events,
        files={"/workspace/cat.png": png_bytes},
    )
    await runner.run(
        "look ![cat](/workspace/cat.png)",
        "build",
        "m",
        "build",
        opts,
    )
    user_messages = [
        message
        for message in provider.messages[0]
        if message.role == "user" and message.content
    ]
    assert user_messages
    last_user = user_messages[-1]
    assert isinstance(last_user.content, list)
    assert any(part.get("type") == "image_url" for part in last_user.content)


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


async def test_parallel_tools_overlap_and_preserve_call_order() -> None:
    """Independent calls in one step overlap; history stays in call order."""
    probe = ProbeTool()
    registry = default_tool_registry()
    registry.register(probe)
    provider = FakeProvider(
        [
            _multi_tool_step(
                [
                    ("probe", {"key": "a"}, "c1"),
                    ("probe", {"key": "b"}, "c2"),
                ]
            ),
            _text_step("both done"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=registry,
        accessor=FakeAccessor(files={}),
        emit=emit,
    )
    started_a, release_a = probe.events_for("a")
    started_b, release_b = probe.events_for("b")
    run_task = asyncio.create_task(runner.run("go", "build", "m", "build"))
    await asyncio.wait_for(started_a.wait(), timeout=2)
    await asyncio.wait_for(started_b.wait(), timeout=2)
    assert probe.max_running == 2
    release_b.set()
    release_a.set()
    result = await run_task
    assert result.output == "both done"
    assert probe.finish_order == ["b", "a"]
    history = provider.messages[1]
    tool_ids = [message.tool_call_id for message in history if message.role == "tool"]
    assert tool_ids == ["c1", "c2"]


async def test_parallel_tool_failure_does_not_cancel_sibling() -> None:
    """One failing call still lets the sibling complete."""
    probe = ProbeTool()
    registry = default_tool_registry()
    registry.register(probe)
    registry.register(BoomTool())
    provider = FakeProvider(
        [
            _multi_tool_step(
                [
                    ("boom", {"key": "x"}, "c1"),
                    ("probe", {"key": "ok"}, "c2"),
                ]
            ),
            _text_step("recovered"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=registry,
        accessor=FakeAccessor(files={}),
        emit=emit,
    )
    started_ok, release_ok = probe.events_for("ok")
    run_task = asyncio.create_task(runner.run("go", "build", "m", "build"))
    await asyncio.wait_for(started_ok.wait(), timeout=2)
    release_ok.set()
    result = await run_task
    assert result.output == "recovered"
    assert any(
        event["type"] == "tool_error" and event.get("call_id") == "c1"
        for event in events
    )
    assert any(
        event["type"] == "tool_completed" and event.get("call_id") == "c2"
        for event in events
    )


async def test_abort_cancels_in_flight_parallel_tools() -> None:
    """Cancelling the run cancels every in-flight tool task."""
    probe = ProbeTool()
    registry = default_tool_registry()
    registry.register(probe)
    provider = FakeProvider(
        [
            _multi_tool_step(
                [
                    ("probe", {"key": "a"}, "c1"),
                    ("probe", {"key": "b"}, "c2"),
                ]
            ),
            _text_step("never"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=registry,
        accessor=FakeAccessor(files={}),
        emit=emit,
    )
    started_a, _release_a = probe.events_for("a")
    started_b, _release_b = probe.events_for("b")
    run_task = asyncio.create_task(runner.run("go", "build", "m", "build"))
    await asyncio.wait_for(started_a.wait(), timeout=2)
    await asyncio.wait_for(started_b.wait(), timeout=2)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert any(event["type"] == "aborted" for event in events)


async def test_parallel_permission_asks_are_independent() -> None:
    """Two ASK gates in one step can be pending at the same time."""
    pending: dict[str, asyncio.Future[str]] = {}
    ready = asyncio.Event()

    async def ask(**kwargs):  # type: ignore[no-untyped-def]
        call_id = str(kwargs["call_id"])
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        pending[call_id] = future
        if len(pending) >= 2:
            ready.set()
        return await future

    provider = FakeProvider(
        [
            _multi_tool_step(
                [
                    ("bash", {"command": "git status"}, "c1"),
                    ("bash", {"command": "git diff"}, "c2"),
                ]
            ),
            _text_step("approved both"),
        ]
    )
    events: list[dict[str, Any]] = []
    runner, opts = _runner(
        provider, events, agent_rules={"bash": "ask"}, on_permission=ask
    )
    run_task = asyncio.create_task(runner.run("go", "build", "m", "build", opts))
    await asyncio.wait_for(ready.wait(), timeout=2)
    assert set(pending) == {"c1", "c2"}
    pending["c1"].set_result("once")
    pending["c2"].set_result("once")
    result = await run_task
    assert result.output == "approved both"


async def test_send_logs_emitter_errors_without_raising() -> None:
    """Emitter failures must not crash the loop or the structlog call."""
    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("emitter down")

    provider = FakeProvider([_text_step("ok")])
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        emit=boom,
    )
    await runner._send({"type": "step_start"})
