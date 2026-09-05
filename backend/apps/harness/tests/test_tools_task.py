"""Tests for the M5 task (subagent) tool and todo events."""

from __future__ import annotations

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
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import TaskTool, default_tool_registry
from apps.harness.tools.base import ToolContext, ToolError


class FakeProvider(ProviderAdapter):
    """Scripted provider with canned steps and offered-tool records."""

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
        self.calls.append({"model": model, "tools": [t.name for t in tools]})
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        for delta in step:
            yield delta


def _text_step(text: str) -> list[Delta]:
    return [Delta(text=text, usage=Usage(1, 1, 2))]


def _tool_step(tool: str, args: dict[str, Any], call_id: str = "c1") -> list[Delta]:
    return [
        Delta(
            tool_calls=(
                {
                    "index": 0,
                    "id": call_id,
                    "name": tool,
                    "arguments": json.dumps(args),
                },
            ),
            usage=Usage(1, 1, 2),
        )
    ]


def _ctx(
    accessor: FakeAccessor, events: list[dict[str, Any]], **kwargs: Any
) -> ToolContext:
    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    params: dict[str, Any] = {
        "session_id": "sess-parent",
        "workspace_id": "ws-1",
        "accessor": accessor,
        "model": "parent-model",
        "parent_emit": _emit,
        "provider": kwargs.pop("provider", None),
        "registry": kwargs.pop("registry", None),
        "evaluator": kwargs.pop("evaluator", None),
    }
    params.update(kwargs)
    return ToolContext(**params)


def _task_ctx(
    accessor: FakeAccessor,
    events: list[dict[str, Any]],
    child_steps: list[list[Delta]],
    **kwargs: Any,
) -> tuple[ToolContext, FakeProvider]:
    provider = FakeProvider(child_steps)
    registry = kwargs.pop("registry", None) or default_tool_registry()
    ctx = _ctx(
        accessor,
        events,
        provider=provider,
        registry=registry,
        evaluator=PermissionEvaluator(),
        **kwargs,
    )
    return ctx, provider


async def _collect_emit(events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    """Append *event* to *events* (awaitable emitter for runner tests)."""
    events.append(event)


def _runner_with_events(
    provider: FakeProvider,
    accessor: FakeAccessor,
    events: list[dict[str, Any]],
    registry=None,
) -> HarnessRunner:
    """Build a HarnessRunner wired to an awaitable list emitter."""

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    return HarnessRunner(
        provider=provider,
        tools=registry or default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=accessor,
        emit=_emit,
    )


async def test_task_happy_path_returns_child_text(fake_accessor) -> None:
    """Parent task call returns the child run text as tool output."""
    events: list[dict[str, Any]] = []
    ctx, _ = _task_ctx(fake_accessor, events, [_text_step("child says hi")])
    result = await TaskTool().execute(
        {"description": "research", "prompt": "look around"}, ctx
    )
    assert result.output == "child says hi"
    kinds = [event["type"] for event in events]
    assert "subtask_started" in kinds
    assert "subtask_finished" in kinds
    finished = next(e for e in events if e["type"] == "subtask_finished")
    assert finished["status"] == "completed"
    assert finished["agent"] == "general"


async def test_task_rejects_unknown_and_primary_agents(fake_accessor) -> None:
    """Unknown, primary, and hidden agent names are rejected."""
    events: list[dict[str, Any]] = []
    ctx, _ = _task_ctx(fake_accessor, events, [_text_step("x")])
    tool = TaskTool()
    for bad in ("build", "plan", "title", "compaction", "nope"):
        with pytest.raises(ToolError, match="Unknown subagent"):
            await tool.execute(
                {"description": "d", "prompt": "p", "subagent_type": bad}, ctx
            )
    with pytest.raises(ToolError, match="Unknown subagent"):
        await tool.execute({"description": "d", "prompt": "p", "agent": "build"}, ctx)


async def test_task_depth_limit_blocks_nested_task(fake_accessor) -> None:
    """Direct calls at depth >= max_depth are rejected (no recursion)."""
    events: list[dict[str, Any]] = []
    ctx, _ = _task_ctx(fake_accessor, events, [_text_step("x")], depth=1, max_depth=1)
    with pytest.raises(ToolError, match="depth limit"):
        await TaskTool().execute({"description": "d", "prompt": "p"}, ctx)
    assert not [e for e in events if e["type"] == "subtask_started"]


async def test_task_in_task_blocked_by_runner_loop(fake_accessor) -> None:
    """A task call smuggled into a max-depth child is denied by the loop."""
    child_provider = FakeProvider(
        [
            _tool_step("task", {"description": "nested", "prompt": "deep"}),
            _text_step("child done"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=child_provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    result = await runner.run(
        "p", "build", "m", "build", RunOptions(depth=1, max_depth=1)
    )
    assert result.output == "child done"
    errors = [e for e in events if e["type"] == "tool_error"]
    assert errors and "depth limit" in errors[0]["error"]


async def test_task_withholds_task_and_todowrite_from_child(fake_accessor) -> None:
    """Child runs never offer task or todowrite to the provider."""
    events: list[dict[str, Any]] = []
    ctx, provider = _task_ctx(fake_accessor, events, [_text_step("researched")])
    await TaskTool().execute(
        {"description": "d", "prompt": "p", "subagent_type": "explore"}, ctx
    )
    offered = provider.calls[0]["tools"]
    assert "task" not in offered
    assert "todowrite" not in offered


async def test_explore_child_is_read_only(fake_accessor) -> None:
    """Explore child denies edit via permissions (deny path, no ask)."""
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    provider = FakeProvider(
        [
            _tool_step("edit", {"path": "a.txt", "a": "b"}),
            _text_step("refused, done"),
        ]
    )
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    result = await runner.run(
        "research", "explore", "m", "build", RunOptions(depth=1, max_depth=1)
    )
    assert result.output == "refused, done"
    assert "edit" not in provider.calls[0]["tools"]
    assert "read" in provider.calls[0]["tools"]


async def test_task_child_failure_is_tool_error(fake_accessor) -> None:
    """Child provider errors surface as ToolError, not a parent crash."""

    class _BoomProvider(FakeProvider):
        async def chat_stream(  # type: ignore[no-untyped-def]
            self, model, messages, tools, opts=None
        ):  # noqa: ANN001, ANN202
            raise RuntimeError("child exploded")
            yield Delta(text="never")

    events: list[dict[str, Any]] = []
    ctx = _ctx(
        fake_accessor,
        events,
        provider=_BoomProvider([_text_step("x")]),
        registry=default_tool_registry(),
        evaluator=PermissionEvaluator(),
    )
    with pytest.raises(ToolError, match="failed"):
        await TaskTool().execute({"description": "d", "prompt": "p"}, ctx)
    finished = [e for e in events if e["type"] == "subtask_finished"]
    assert finished and finished[0]["status"] == "error"


async def test_task_inherits_parent_model(fake_accessor) -> None:
    """Child runs use the parent model unless model_override is given."""
    events: list[dict[str, Any]] = []
    ctx, provider = _task_ctx(fake_accessor, events, [_text_step("ok")])
    assert ctx.model == "parent-model"
    await TaskTool().execute({"description": "d", "prompt": "p"}, ctx)
    assert provider.calls[0]["model"] == "parent-model"
    events2: list[dict[str, Any]] = []
    ctx2, provider2 = _task_ctx(fake_accessor, events2, [_text_step("ok")])
    await TaskTool().execute(
        {"description": "d", "prompt": "p", "model_override": "child-model"},
        ctx2,
    )
    assert provider2.calls[0]["model"] == "child-model"


async def test_task_long_output_truncated(fake_accessor) -> None:
    """Oversized child results are truncated with a marker."""
    from apps.harness.tools.subagents import TASK_OUTPUT_MAX_CHARS

    events: list[dict[str, Any]] = []
    ctx, _ = _task_ctx(
        fake_accessor, events, [_text_step("y" * (TASK_OUTPUT_MAX_CHARS + 10))]
    )
    result = await TaskTool().execute({"description": "d", "prompt": "p"}, ctx)
    assert result.truncated is True
    assert "truncated" in result.output


async def test_todo_updated_event_after_todowrite(fake_accessor) -> None:
    """todowrite completion emits todo_updated with the stored list."""
    from apps.harness.tools.todos import InMemoryTodoRepository

    repo = InMemoryTodoRepository()
    registry = default_tool_registry()
    registry.get("todowrite")._repository = repo  # type: ignore[attr-defined]
    provider = FakeProvider(
        [
            _tool_step(
                "todowrite",
                {"todos": [{"content": "do it", "status": "in_progress"}]},
            ),
            _text_step("noted"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def _emit(event):
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=registry,
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    result = await runner.run(
        "track", "build", "m", "build", RunOptions(session_id="sess-todo")
    )
    assert result.output == "noted"
    updated = [e for e in events if e["type"] == "todo_updated"]
    assert updated
    assert updated[0]["todos"][0]["content"] == "do it"
    assert updated[0]["todos"][0]["status"] == "in_progress"


async def test_runner_withholds_task_at_max_depth(fake_accessor) -> None:
    """At depth >= max_depth the loop withholds task from the provider."""
    provider = FakeProvider([_text_step("done")])
    events: list[dict[str, Any]] = []

    async def _emit(event):
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    await runner.run("p", "build", "m", "build", RunOptions(depth=1, max_depth=1))
    assert "task" not in provider.calls[0]["tools"]
    assert "read" in provider.calls[0]["tools"]


async def test_runner_allows_task_below_max_depth(fake_accessor) -> None:
    """At depth 0 with max_depth 2 the task tool is still offered."""
    provider = FakeProvider([_text_step("done")])
    events: list[dict[str, Any]] = []

    async def _emit(event):
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    await runner.run("p", "build", "m", "build", RunOptions(depth=0, max_depth=2))
    assert "task" in provider.calls[0]["tools"]
    assert "todowrite" in provider.calls[0]["tools"]


async def test_runner_denies_direct_nested_task_call(fake_accessor) -> None:
    """A task tool call smuggled to depth 1 errors without executing."""
    provider = FakeProvider(
        [
            _tool_step("task", {"description": "nested", "prompt": "deep"}),
            _text_step("stopped"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def _emit(event):
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    result = await runner.run(
        "p", "build", "m", "build", RunOptions(depth=1, max_depth=1)
    )
    assert result.output == "stopped"
    errors = [e for e in events if e["type"] == "tool_error"]
    assert errors and "depth limit" in errors[0]["error"]


async def test_runner_denies_todowrite_in_child(fake_accessor) -> None:
    """todowrite calls at depth > 0 are denied without touching the store."""
    provider = FakeProvider(
        [
            _tool_step(
                "todowrite",
                {"todos": [{"content": "sneaky", "status": "pending"}]},
            ),
            _text_step("no todos here"),
        ]
    )
    events: list[dict[str, Any]] = []

    async def _emit(event):
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=_emit,
    )
    result = await runner.run(
        "p", "build", "m", "build", RunOptions(depth=1, max_depth=2)
    )
    assert result.output == "no todos here"
    assert "todowrite" not in provider.calls[0]["tools"]
    errors = [e for e in events if e["type"] == "tool_error"]
    assert errors and "todowrite" in errors[0]["error"]
    assert not [e for e in events if e["type"] == "todo_updated"]
