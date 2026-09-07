"""Standard tool registration for the agent harness."""

from __future__ import annotations

from .base import ToolRegistry
from .computeruse import COMPUTER_USE_TOOL_NAMES, computeruse_tools
from .files import EditTool, ReadTool, WriteTool
from .process import (
    ProcessGetTool,
    ProcessListTool,
    ProcessStartTool,
    ProcessStopTool,
)
from .question import QuestionTool
from .shell import BashTool, GlobTool, GrepTool, ListTool
from .subagents import TaskTool, WebfetchTool
from .todos import TodoWriteTool


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
    ):
        registry.register(tool)
    return registry


def computeruse_tool_registry() -> ToolRegistry:
    """Build a registry with only computer-use tools."""
    registry = ToolRegistry()
    for tool in computeruse_tools():
        registry.register(tool)
    return registry


__all__ = [
    "BashTool",
    "COMPUTER_USE_TOOL_NAMES",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ListTool",
    "ProcessGetTool",
    "ProcessListTool",
    "ProcessStartTool",
    "ProcessStopTool",
    "QuestionTool",
    "ReadTool",
    "TaskTool",
    "TodoWriteTool",
    "WebfetchTool",
    "WriteTool",
    "computeruse_tool_registry",
    "default_tool_registry",
]
