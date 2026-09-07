"""Tests for the background-process harness tools."""

from __future__ import annotations

import pytest

from apps.harness.access.runner_accessor import RunnerAccessorError
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import (
    ProcessGetTool,
    ProcessListTool,
    ProcessStartTool,
    ProcessStopTool,
    default_tool_registry,
)
from apps.harness.tools.base import ToolContext, ToolError


def _ctx(accessor: FakeAccessor) -> ToolContext:
    return ToolContext(session_id="sess-1", workspace_id="ws-1", accessor=accessor)


def test_permission_keys_are_process() -> None:
    """All four tools gate on the shared ``process`` permission key."""
    assert ProcessStartTool().permission_key == "process"
    assert ProcessListTool().permission_key == "process"
    assert ProcessGetTool().permission_key == "process"
    assert ProcessStopTool().permission_key == "process"


async def test_process_start_happy_path(fake_accessor) -> None:
    """process_start returns the id, pid and log path hint."""
    tool = ProcessStartTool()
    result = await tool.execute(
        {"command": "python server.py", "name": "web"}, _ctx(fake_accessor)
    )
    assert "proc-1" in result.output
    assert "read /workspace/.opencuria/processes/proc-1.log" in result.output
    assert result.metadata["process_id"] == "proc-1"
    assert result.metadata["pid"] == 1234
    assert result.metadata["status"] == "running"
    assert tool.title(tool.coerce_args({"command": "python server.py"})) == (
        "Start python server.py"
    )


async def test_process_start_rejects_empty_command(fake_accessor) -> None:
    """Empty commands fail fast with ToolError."""
    with pytest.raises(ToolError, match="command must not be empty"):
        await tool_start().execute({"command": "  "}, _ctx(fake_accessor))


async def test_process_start_allowed_env_passes() -> None:
    """Benign env vars reach the accessor; missing env means {}."""
    accessor = FakeAccessor()
    result = await ProcessStartTool().execute(
        {"command": "sleep 60", "env": {"FOO": "1"}},
        _ctx(accessor),
    )
    assert result.metadata["process_id"] == "proc-1"


@pytest.mark.parametrize("key", ["LD_PRELOAD", "PATH", "PYTHONPATH", "HOME"])
async def test_process_start_blocked_env_rejected(
    key: str, fake_accessor: FakeAccessor
) -> None:
    """Dangerous env keys are rejected before any accessor call."""
    before = dict(fake_accessor.processes)
    with pytest.raises(ToolError, match="blocked"):
        await ProcessStartTool().execute(
            {"command": "sleep 60", "env": {key: "evil"}},
            _ctx(fake_accessor),
        )
    assert fake_accessor.processes == before


def tool_start() -> ProcessStartTool:
    """Return a fresh ProcessStartTool."""
    return ProcessStartTool()


async def test_process_list_happy_path_and_empty() -> None:
    """process_list renders rows or the empty message."""
    empty = FakeAccessor()
    result = await ProcessListTool().execute({}, _ctx(empty))
    assert result.output == "No background processes running."
    assert result.metadata == {"count": 0, "processes": []}

    accessor = FakeAccessor()
    await accessor.process_start("sleep 60")
    result = await ProcessListTool().execute({}, _ctx(accessor))
    assert "proc-1" in result.output
    assert "sleep 60" in result.output
    assert result.metadata["count"] == 1


async def test_process_get_happy_path(fake_accessor) -> None:
    """process_get returns the status line plus full metadata."""
    await fake_accessor.process_start("sleep 60")
    result = await ProcessGetTool().execute(
        {"process_id": "proc-1"}, _ctx(fake_accessor)
    )
    assert "proc-1" in result.output
    assert result.metadata["process_id"] == "proc-1"

    with pytest.raises(ToolError, match="process_id must not be empty"):
        await ProcessGetTool().execute({"process_id": "  "}, _ctx(fake_accessor))


async def test_process_stop_happy_path(fake_accessor) -> None:
    """process_stop reports already-exited for finished records."""
    await fake_accessor.process_start("sleep 60")
    result = await ProcessStopTool().execute(
        {"process_id": "proc-1"}, _ctx(fake_accessor)
    )
    assert "proc-1" in result.output
    assert result.metadata["status"] == "exited"


async def test_process_tools_translate_runner_errors() -> None:
    """Runner failures surface as ToolError for every tool."""
    failing = FakeAccessor(error=RunnerAccessorError("runner offline"))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessStartTool().execute({"command": "sleep 1"}, _ctx(failing))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessListTool().execute({}, _ctx(failing))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessGetTool().execute({"process_id": "proc-1"}, _ctx(failing))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessStopTool().execute({"process_id": "proc-1"}, _ctx(failing))


async def test_default_registry_contains_process_tools() -> None:
    """The default registry exposes all four process tools."""
    registry = default_tool_registry()
    for name in ("process_start", "process_list", "process_get", "process_stop"):
        assert name in registry
    assert registry.get("process_start").permission_key == "process"
