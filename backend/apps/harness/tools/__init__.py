"""Standard tool registration for the agent harness."""

from __future__ import annotations

from .base import ToolRegistry
from .files import EditTool, ReadTool, WriteTool
from .process import (
    ProcessDeleteTool,
    ProcessGetTool,
    ProcessListTool,
    ProcessRestartTool,
    ProcessStartTool,
    ProcessStopTool,
)
from .question import QuestionTool
from .shell import BashTool, GlobTool, GrepTool, ListTool
from .subagents import TaskTool
from .todos import TodoWriteTool
from .webfetch import WebfetchTool


def default_tool_registry() -> ToolRegistry:
    """Build a registry with all nine standard tools plus webfetch."""
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GlobTool(),
        GrepTool(),
        ListTool(),
        TodoWriteTool(),
        QuestionTool(),
        TaskTool(),
        WebfetchTool(),
        ProcessStartTool(),
        ProcessListTool(),
        ProcessGetTool(),
        ProcessStopTool(),
        ProcessRestartTool(),
        ProcessDeleteTool(),
    ):
        registry.register(tool)
    return registry


def agent_s_tool_registry() -> ToolRegistry:
    """Build the tool registry for ``computeruse`` (Agent-S) children.

    Agent-S plans never see OpenCuria tool schemas: the registry is
    intentionally empty so ``HarnessRunner._filtered_schemas`` offers
    ``tools=[]`` and the Agent-S wire uses no function calls. Hooks are
    copied by the caller (see ``tools.subagents._child_registry``).
    """
    return ToolRegistry()


def computeruse_tool_registry() -> ToolRegistry:
    """Backwards-compatible alias for :func:`agent_s_tool_registry`."""
    return agent_s_tool_registry()


__all__ = [
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ListTool",
    "ProcessDeleteTool",
    "ProcessGetTool",
    "ProcessListTool",
    "ProcessRestartTool",
    "ProcessStartTool",
    "ProcessStopTool",
    "QuestionTool",
    "ReadTool",
    "TaskTool",
    "TodoWriteTool",
    "WebfetchTool",
    "WriteTool",
    "agent_s_tool_registry",
    "computeruse_tool_registry",
    "default_tool_registry",
]
