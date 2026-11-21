"""Standard tool registration for the agent harness."""

from __future__ import annotations

from ..access.guest import LINUX_GUEST, GuestPlatform
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


def default_tool_registry(guest: GuestPlatform = LINUX_GUEST) -> ToolRegistry:
    """Build a registry with all nine standard tools plus webfetch.

    Tool descriptions match the guest OS of the workspace the run uses.
    """
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
        ProcessStopTool(guest),
        ProcessRestartTool(),
        ProcessDeleteTool(),
    ):
        registry.register(tool)
    return registry


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
    "default_tool_registry",
]
