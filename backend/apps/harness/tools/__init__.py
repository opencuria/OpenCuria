"""Standard tool registration for the agent harness."""

from __future__ import annotations

from .base import ToolRegistry
from .files import EditTool, ReadTool, WriteTool
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
    ):
        registry.register(tool)
    return registry


__all__ = [
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ListTool",
    "QuestionTool",
    "ReadTool",
    "TaskTool",
    "TodoWriteTool",
    "WebfetchTool",
    "WriteTool",
    "default_tool_registry",
]
