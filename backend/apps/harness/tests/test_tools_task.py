"""Tests for the M5 task (subagent) tool and todo events."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from apps.harness.engines.base import RunOptions
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.harness.runner import HarnessRunner
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.todos.memory_repository import InMemoryTodoRepository
from apps.harness.todos.service import TodoService
from apps.harness.tools import TaskTool, TodoWriteTool, default_tool_registry
from apps.harness.tools.base import ToolContext, ToolError, ToolResult
from apps.harness.tools.subagents import (
    TASK_OUTPUT_MAX_CHARS,
    TaskArgs,
    build_task_result,
)


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
        """Yield the next canned step; then a text-only done reply."""
        self.calls.append({"model": model, "tools": [t.name for t in tools]})
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0)
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


def _ctx(accessor: FakeAccessor, **kwargs: Any) -> ToolContext:
    return ToolContext(
        session_id="sess-parent",
        workspace_id="ws-1",
        accessor=accessor,
        **kwargs,
    )


def test_task_tool_description_mentions_parallel_calls() -> None:
    """Task tool tells the model to launch independent calls in one message."""
    assert "one message" in TaskTool.description
    assert "parallel" in TaskTool.description


async def test_task_dispatches_callback(fake_accessor: FakeAccessor) -> None:
    """Validated arguments and the service result pass through unchanged."""
    expected = ToolResult(output="child says hi", metadata={"task_id": "child"})
    callback = AsyncMock(return_value=expected)
    ctx = _ctx(fake_accessor, run_subagent=callback)
    args = TaskArgs(
        description="research",
        prompt="look around",
        agent="explore",
        task_id="existing-child",
        model_override="child-model",
    )

    result = await TaskTool().execute(args.model_dump(), ctx)

    assert result is expected
    callback.assert_awaited_once()
    dispatched_args, dispatched_ctx, subtask_id = callback.await_args.args
    assert dispatched_args == args
    assert dispatched_ctx is ctx
    assert re.fullmatch(r"[0-9a-f]+", subtask_id)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("child exploded"),
        ToolError("service refused"),
        asyncio.CancelledError(),
    ],
)
async def test_task_callback_exceptions_propagate(
    fake_accessor: FakeAccessor, error: BaseException
) -> None:
    """The service owns error handling, including cancellation semantics."""
    callback = AsyncMock(side_effect=error)
    ctx = _ctx(fake_accessor, run_subagent=callback)
    with pytest.raises(type(error)) as raised:
        await TaskTool().execute({"description": "d", "prompt": "p"}, ctx)
    assert raised.value is error
    callback.assert_awaited_once()


async def test_task_rejects_unknown_and_primary_agents(fake_accessor) -> None:
    """Unknown, primary, and hidden agent names are rejected."""
    callback = AsyncMock()
    ctx = _ctx(fake_accessor, run_subagent=callback)
    tool = TaskTool()
    for bad in ("build", "plan", "title", "compaction", "nope"):
        with pytest.raises(ToolError, match="Unknown subagent"):
            await tool.execute(
                {"description": "d", "prompt": "p", "subagent_type": bad}, ctx
            )
    with pytest.raises(ToolError, match="Unknown subagent"):
        await tool.execute({"description": "d", "prompt": "p", "agent": "build"}, ctx)
    callback.assert_not_awaited()


async def test_task_depth_limit_blocks_nested_task(fake_accessor) -> None:
    """Direct calls at depth >= max_depth are rejected (no recursion)."""
    callback = AsyncMock()
    ctx = _ctx(fake_accessor, run_subagent=callback, depth=1, max_depth=1)
    with pytest.raises(ToolError, match="depth limit"):
        await TaskTool().execute({"description": "d", "prompt": "p"}, ctx)
    callback.assert_not_awaited()


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


@pytest.mark.parametrize("many_lines", [False, True])
async def test_task_long_output_spills_full_answer(
    fake_accessor: FakeAccessor, many_lines: bool
) -> None:
    """Long answers retain one complete, readable UTF-8 copy on disk."""
    output = ("ä\n" * 6000 if many_lines else "ä" * 9000) + "\nmissing conclusion"
    result = await build_task_result(
        "child", output, _ctx(fake_accessor), agent="general", metadata={}
    )
    assert result.truncated is True
    assert result.metadata["truncated"] is True
    assert "truncated" in result.output
    assert '<task id="' in result.output
    path = result.metadata["output_path"]
    assert result.output.count(f"Full output: {path}.") == 1
    assert "Use read with offset/limit or grep" in result.output
    assert "missing conclusion" not in result.output
    assert fake_accessor.written == {path: output.encode("utf-8")}
    assert len(result.output) < TASK_OUTPUT_MAX_CHARS + 1000


@pytest.mark.parametrize("many_lines", [False, True])
async def test_task_spill_failure_keeps_preview(
    fake_accessor: FakeAccessor, monkeypatch: pytest.MonkeyPatch, many_lines: bool
) -> None:
    """A failed spill never fails the task or points at an incomplete answer."""
    attempts: list[bytes] = []

    async def fail_write(path: str, content: bytes, mode: int = 0o644) -> None:
        attempts.append(content)
        raise PermissionError("write denied")

    monkeypatch.setattr(fake_accessor, "write_file", fail_write)
    output = ("x\n" * 6000 if many_lines else "x" * 9000) + "\nconclusion"
    result = await build_task_result(
        "child", output, _ctx(fake_accessor), agent="general", metadata={}
    )
    assert result.truncated is True
    assert '<task id="' in result.output
    assert "truncated" in result.output
    assert "Full output:" not in result.output
    assert "output_path" not in result.metadata
    assert attempts == [output.encode("utf-8")]
    assert not fake_accessor.written


@pytest.mark.parametrize("recorded", [False, True])
async def test_task_spill_preserves_computeruse_recording(
    fake_accessor: FakeAccessor, recorded: bool
) -> None:
    """The preview keeps the real recording and the file keeps the full text."""
    marker = "![Computer use](/workspace/.opencuria/computeruse/child/session.mp4)"
    output = "x" * 9000 + (f"\n\n{marker}" if recorded else "")
    result = await build_task_result(
        "child", output, _ctx(fake_accessor), agent="computeruse", metadata={}
    )
    assert result.truncated is True
    assert result.output.count(marker) == int(recorded)
    path = result.metadata["output_path"]
    assert fake_accessor.files[path].decode("utf-8") == output


async def test_task_envelope_carries_resumable_task_id(fake_accessor) -> None:
    """Bounded task output embeds the persistent task_id for resume."""
    task_id = "persisted-child"
    result = await build_task_result(
        task_id,
        "hello",
        _ctx(fake_accessor),
        agent="general",
        metadata={"task_id": task_id},
    )
    assert result.metadata["task_id"] == task_id
    assert result.truncated is False
    assert not fake_accessor.written
    assert f'<task id="{task_id}" state="completed">' in result.output
    assert "<task_result>" in result.output
    assert "hello" in result.output


async def test_todo_updated_event_after_todowrite(fake_accessor) -> None:
    """todowrite completion emits todo_updated with the stored list."""
    repo = InMemoryTodoRepository()
    registry = default_tool_registry()
    registry._tools["todowrite"] = TodoWriteTool(service=TodoService(repository=repo))
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
