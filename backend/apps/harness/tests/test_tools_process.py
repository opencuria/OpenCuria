"""Tests for the background-process harness tools."""

from __future__ import annotations

import pytest

from apps.harness.access.runner_accessor import RunnerAccessorError
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import (
    ProcessDeleteTool,
    ProcessGetTool,
    ProcessListTool,
    ProcessRestartTool,
    ProcessStartTool,
    ProcessStopTool,
    default_tool_registry,
)
from apps.harness.tools.base import ToolContext, ToolError


def _ctx(accessor: FakeAccessor) -> ToolContext:
    return ToolContext(session_id="sess-1", workspace_id="ws-1", accessor=accessor)


def test_permission_keys_are_process() -> None:
    """All six tools gate on the shared ``process`` permission key."""
    assert ProcessStartTool().permission_key == "process"
    assert ProcessListTool().permission_key == "process"
    assert ProcessGetTool().permission_key == "process"
    assert ProcessStopTool().permission_key == "process"
    assert ProcessRestartTool().permission_key == "process"
    assert ProcessDeleteTool().permission_key == "process"


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
    assert result.metadata["name"] == "web"
    assert result.metadata["run_count"] == 1
    assert tool.title(tool.coerce_args({"command": "python server.py", "name": "web"})) == (
        "Start python server.py"
    )


async def test_process_start_same_name_restarts_in_place(fake_accessor) -> None:
    """Starting the same name twice reports a restart with run count +1."""
    tool = ProcessStartTool()
    await tool.execute(
        {"command": "python server.py", "name": "web"}, _ctx(fake_accessor)
    )
    second = await tool.execute(
        {"command": "python server.py --reload", "name": "web"},
        _ctx(fake_accessor),
    )
    assert "Restarted" in second.output
    assert "run 2" in second.output
    assert second.metadata["process_id"] == "proc-1"
    assert second.metadata["run_count"] == 2


async def test_process_start_rejects_empty_command(fake_accessor) -> None:
    """Empty commands fail fast with ToolError."""
    with pytest.raises(ToolError, match="command must not be empty"):
        await tool_start().execute(
            {"command": "  ", "name": "web"}, _ctx(fake_accessor)
        )


async def test_process_start_requires_name(fake_accessor) -> None:
    """Missing or blank names fail fast with ToolError."""
    with pytest.raises(ToolError, match="name"):
        await tool_start().execute({"command": "sleep 60"}, _ctx(fake_accessor))
    with pytest.raises(ToolError, match="name must not be empty"):
        await tool_start().execute(
            {"command": "sleep 60", "name": "  "}, _ctx(fake_accessor)
        )


async def test_process_start_allowed_env_passes() -> None:
    """Benign env vars reach the accessor; missing env means {}."""
    accessor = FakeAccessor()
    result = await ProcessStartTool().execute(
        {"command": "sleep 60", "name": "web", "env": {"FOO": "1"}},
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
            {"command": "sleep 60", "name": "web", "env": {key: "evil"}},
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
    await accessor.process_start("sleep 60", name="web")
    result = await ProcessListTool().execute({}, _ctx(accessor))
    assert "proc-1" in result.output
    assert "web" in result.output
    assert "sleep 60" in result.output
    assert result.metadata["count"] == 1


async def test_process_get_happy_path(fake_accessor) -> None:
    """process_get returns the status line plus full metadata."""
    await fake_accessor.process_start("sleep 60", name="web")
    result = await ProcessGetTool().execute(
        {"process_id": "proc-1"}, _ctx(fake_accessor)
    )
    assert "proc-1" in result.output
    assert result.metadata["process_id"] == "proc-1"

    with pytest.raises(ToolError, match="process_id must not be empty"):
        await ProcessGetTool().execute({"process_id": "  "}, _ctx(fake_accessor))


async def test_process_get_resolves_name(fake_accessor) -> None:
    """process_get accepts the exact process name as well as the UUID."""
    await fake_accessor.process_start("sleep 60", name="web")
    result = await ProcessGetTool().execute(
        {"process_id": "web"}, _ctx(fake_accessor)
    )
    assert result.metadata["process_id"] == "proc-1"
    assert "web" in result.output


async def test_process_stop_happy_path(fake_accessor) -> None:
    """process_stop reports already-exited for finished records."""
    await fake_accessor.process_start("sleep 60", name="web")
    result = await ProcessStopTool().execute(
        {"process_id": "proc-1"}, _ctx(fake_accessor)
    )
    assert "proc-1" in result.output
    assert result.metadata["status"] == "exited"


async def test_process_stop_resolves_name(fake_accessor) -> None:
    """process_stop accepts the exact process name."""
    await fake_accessor.process_start("sleep 60", name="web")
    result = await ProcessStopTool().execute(
        {"process_id": "web"}, _ctx(fake_accessor)
    )
    assert result.metadata["status"] == "exited"


async def test_process_restart_happy_path(fake_accessor) -> None:
    """process_restart bumps the run count and returns a new log path."""
    await fake_accessor.process_start("sleep 60", name="web")
    result = await ProcessRestartTool().execute(
        {"process_id": "proc-1"}, _ctx(fake_accessor)
    )
    assert "Restarted" in result.output
    assert "run 2" in result.output
    assert result.metadata["process_id"] == "proc-1"
    assert result.metadata["run_count"] == 2
    assert result.metadata["status"] == "running"
    assert "_r2.log" in result.output

    by_name = await ProcessRestartTool().execute(
        {"process_id": "web"}, _ctx(fake_accessor)
    )
    assert by_name.metadata["run_count"] == 3

    with pytest.raises(ToolError, match="process_id must not be empty"):
        await ProcessRestartTool().execute(
            {"process_id": "  "}, _ctx(fake_accessor)
        )


async def test_process_delete_happy_path(fake_accessor) -> None:
    """process_delete removes the record and confirms the id."""
    await fake_accessor.process_start("sleep 60", name="web")
    result = await ProcessDeleteTool().execute(
        {"process_id": "proc-1"}, _ctx(fake_accessor)
    )
    assert result.output == "Deleted background process proc-1."
    assert result.metadata == {"process_id": "proc-1", "deleted": True}
    assert fake_accessor.processes == {}

    with pytest.raises(ToolError, match="process_id must not be empty"):
        await ProcessDeleteTool().execute(
            {"process_id": "  "}, _ctx(fake_accessor)
        )


async def test_process_delete_resolves_name() -> None:
    """process_delete accepts the exact process name."""
    accessor = FakeAccessor()
    await accessor.process_start("sleep 60", name="web")
    result = await ProcessDeleteTool().execute(
        {"process_id": "web"}, _ctx(accessor)
    )
    assert result.metadata == {"process_id": "proc-1", "deleted": True}


async def test_process_tools_translate_runner_errors() -> None:
    """Runner failures surface as ToolError for every tool."""
    failing = FakeAccessor(error=RunnerAccessorError("runner offline"))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessStartTool().execute(
            {"command": "sleep 1", "name": "web"}, _ctx(failing)
        )
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessListTool().execute({}, _ctx(failing))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessGetTool().execute({"process_id": "proc-1"}, _ctx(failing))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessStopTool().execute({"process_id": "proc-1"}, _ctx(failing))
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessRestartTool().execute(
            {"process_id": "proc-1"}, _ctx(failing)
        )
    with pytest.raises(ToolError, match="runner offline"):
        await ProcessDeleteTool().execute({"process_id": "proc-1"}, _ctx(failing))


async def test_default_registry_contains_process_tools() -> None:
    """The default registry exposes all six process tools."""
    registry = default_tool_registry()
    for name in (
        "process_start",
        "process_list",
        "process_get",
        "process_stop",
        "process_restart",
        "process_delete",
    ):
        assert name in registry
    assert registry.get("process_start").permission_key == "process"
    assert registry.get("process_restart").permission_key == "process"
    assert registry.get("process_delete").permission_key == "process"
