"""Tests for harness tool ABC, registry and hooks."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from apps.harness.providers.base import ToolSchema
from apps.harness.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)


class _Args(BaseModel):
    """Args for the dummy tool."""

    text: str = Field(default="hi")


class _DummyTool(Tool):
    """Minimal tool for registry tests."""

    name = "dummy"
    description = "Dummy tool."
    args_schema: type[BaseModel] = _Args
    permission_key = "dummy"

    def title(self, args: BaseModel) -> str:
        """Return a static title."""
        return "Dummy"

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Echo the text argument."""
        assert isinstance(args, _Args)
        return ToolResult(output=args.text)


class _FailingTool(_DummyTool):
    """Tool that always fails."""

    name = "failing"

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Raise a tool error."""
        raise ToolError("nope", tool=self.name)


def _ctx(accessor) -> ToolContext:
    return ToolContext(session_id="sess-1", workspace_id="ws-1", accessor=accessor)


def test_registry_register_get_list_names(fake_accessor) -> None:
    """Tools register, resolve, list and report names."""
    registry = ToolRegistry()
    tool = _DummyTool()
    registry.register(tool)
    assert registry.get("dummy") is tool
    assert registry.get("DUMMY") is tool
    assert registry.names() == ["dummy"]
    assert registry.list() == [tool]
    assert "dummy" in registry
    assert "nope" not in registry
    assert 123 not in registry  # type: ignore[operator]


def test_registry_rejects_empty_and_duplicate_names() -> None:
    """Empty names and duplicates are rejected."""
    registry = ToolRegistry()
    nameless = _DummyTool()
    nameless.name = "  "
    with pytest.raises(ValueError, match="non-empty name"):
        registry.register(nameless)
    registry.register(_DummyTool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_DummyTool())


def test_registry_unknown_tool_raises() -> None:
    """Unknown tool lookup raises KeyError."""
    with pytest.raises(KeyError, match="Unknown tool"):
        ToolRegistry().get("nope")


def test_registry_schemas_use_pydantic_json_schema() -> None:
    """Schemas expose pydantic-derived JSON schema per tool."""
    registry = ToolRegistry()
    registry.register(_DummyTool())
    schemas = registry.schemas()
    assert len(schemas) == 1
    schema = schemas[0]
    assert isinstance(schema, ToolSchema)
    assert schema.name == "dummy"
    assert schema.description == "Dummy tool."
    assert schema.parameters["type"] == "object"
    assert "text" in schema.parameters["properties"]


async def test_registry_execute_validates_and_runs(fake_accessor) -> None:
    """execute() validates dict args and returns the tool result."""
    registry = ToolRegistry()
    registry.register(_DummyTool())
    result = await registry.execute("dummy", {"text": "hello"}, _ctx(fake_accessor))
    assert result.output == "hello"


async def test_registry_execute_propagates_tool_error(
    fake_accessor,
) -> None:
    """Tool errors propagate through registry.execute."""
    registry = ToolRegistry()
    registry.register(_FailingTool())
    with pytest.raises(ToolError, match="nope"):
        await registry.execute("failing", {}, _ctx(fake_accessor))


async def test_before_after_hooks_run_in_order(fake_accessor) -> None:
    """Hooks observe invocations without changing the result."""
    registry = ToolRegistry()
    registry.register(_DummyTool())
    calls: list[str] = []

    async def before(name, args, ctx) -> None:
        calls.append(f"before:{name}")

    async def after(name, args, ctx, result) -> None:
        calls.append(f"after:{name}:{result.output}")

    registry.add_before_hook(before)
    registry.add_after_hook(after)
    result = await registry.execute("dummy", {"text": "x"}, _ctx(fake_accessor))
    assert result.output == "x"
    assert calls == ["before:dummy", "after:dummy:x"]


async def test_registry_bash_tail_spills_full_output(fake_accessor) -> None:
    """Bash (tail) spills the full output and hints at the spill path."""
    big = "\n".join(f"line-{i}" for i in range(2100))

    class _Bashish(_DummyTool):
        name = "bashish"
        truncate_direction = "tail"

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolResult(output=big)

    registry = ToolRegistry()
    registry.register(_Bashish())
    result = await registry.execute(
        "bashish", {}, _ctx(fake_accessor)
    )
    assert result.truncated is True
    assert result.output.startswith("...")
    assert "Full output: /workspace/.opencuria/tool-output/" in result.output
    assert result.metadata["output_path"].startswith(
        "/workspace/.opencuria/tool-output/"
    )
    assert result.metadata["output_path"].endswith(".log")
    assert fake_accessor.files[result.metadata["output_path"]].decode() == big


async def test_registry_head_default_spills_with_suffix_hint(fake_accessor) -> None:
    """Head tools spill with a suffix hint and truncated metadata."""
    from apps.harness.tools.truncate import MAX_BYTES as _MAX_BYTES

    big = "y" * (_MAX_BYTES + 100)

    class _Headish(_DummyTool):
        name = "headish"

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolResult(output=big)

    registry = ToolRegistry()
    registry.register(_Headish())
    result = await registry.execute("headish", {}, _ctx(fake_accessor))
    assert result.truncated is True
    assert "\n\nFull output: /workspace/.opencuria/tool-output/" in result.output
    assert result.metadata["truncated"] is True
    assert ".log" in result.metadata["output_path"]


async def test_registry_task_hint_when_subagent_available(fake_accessor) -> None:
    """Spill hint names the task tool when depth allows subagents."""
    from apps.harness.tools.base import ToolContext

    big = "z" * 60000

    class _Taskish(_DummyTool):
        name = "taskish"

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolResult(output=big)

    registry = ToolRegistry()
    registry.register(_Taskish())
    registry.register(_DummyTool())  # dummy has name "dummy"; add task below

    class _TaskTool(_DummyTool):
        name = "task"

    registry.register(_TaskTool())
    ctx = ToolContext(
        session_id="s", workspace_id="w", accessor=fake_accessor,
        depth=0, max_depth=1, registry=registry,
    )
    result = await registry.execute("taskish", {}, ctx)
    assert "Use the task tool (explore agent)" in result.output


async def test_registry_spill_failure_does_not_break_tool(fake_accessor) -> None:
    """Write failures during spill are swallowed; the preview survives."""
    big = "q" * 60000

    class _FailSpill(_DummyTool):
        name = "failspill"

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolResult(output=big)

    class _BrokenAccessor:
        async def write_file(self, path, content, mode=0o644):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk gone")

    from apps.harness.tools.base import ToolContext

    registry = ToolRegistry()
    registry.register(_FailSpill())
    ctx = ToolContext(session_id="s", workspace_id="w", accessor=_BrokenAccessor())  # type: ignore[arg-type]
    result = await registry.execute("failspill", {}, ctx)
    assert result.truncated is True
    assert "Full output:" not in result.output
    assert "output_path" not in result.metadata


async def test_registry_no_spill_when_not_truncated(fake_accessor) -> None:
    """Small outputs never touch the accessor's write path."""
    registry = ToolRegistry()
    registry.register(_DummyTool())
    before = dict(fake_accessor.files)
    result = await registry.execute("dummy", {"text": "hi"}, _ctx(fake_accessor))
    assert result.truncated is False
    assert fake_accessor.files == before
