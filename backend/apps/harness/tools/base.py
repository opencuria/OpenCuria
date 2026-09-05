"""Tool ABC, context, registry and hook extension points.

Every harness tool reaches the workspace exclusively through a
:class:`~apps.harness.access.base.WorkspaceAccessor` (provided via
:class:`ToolContext`). Tools never touch the local filesystem, never spawn
local shells, and never use the ORM directly.

Before/after hooks are an intentionally empty extension point: later
milestones (plugins, MCP) can attach observers without changing tools.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel

from ..access.base import WorkspaceAccessor
from ..providers.base import ToolSchema

log = structlog.get_logger(__name__)


class ToolError(Exception):
    """Raised when a tool cannot fulfill a validly dispatched call."""

    def __init__(self, message: str, *, tool: str = "") -> None:
        self.tool = tool
        super().__init__(message)


@dataclass
class ToolContext:
    """Runtime context handed to every tool execution."""

    session_id: str
    workspace_id: str
    accessor: WorkspaceAccessor
    agent_name: str = ""
    directory: str = "/workspace"


@dataclass
class ToolResult:
    """Structured result returned by every tool."""

    output: str
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


BeforeHook = Callable[[str, BaseModel, ToolContext], Awaitable[None]]
AfterHook = Callable[[str, BaseModel, ToolContext, ToolResult], Awaitable[None]]


class Tool(abc.ABC):
    """Abstract base class for all harness tools."""

    name: str = ""
    description: str = ""
    args_schema: type[BaseModel] = BaseModel
    permission_key: str = ""

    def title(self, args: BaseModel) -> str:
        """Return a short human-readable title for this invocation."""
        return self.name or type(self).__name__

    def coerce_args(self, args: BaseModel | dict[str, Any]) -> BaseModel:
        """Validate *args* against the tool schema.

        Accepts an already-validated model (pass-through) or a raw dict
        (validated via pydantic). Direct ``tool.execute({...})`` calls in
        tests and future callers therefore behave like registry dispatch.
        """
        if isinstance(args, self.args_schema):
            return args
        if isinstance(args, dict):
            return self.args_schema.model_validate(args)
        raise ToolError(
            f"Invalid arguments for tool '{self.name}': {type(args).__name__}",
            tool=self.name,
        )

    @abc.abstractmethod
    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Execute the tool with validated *args* in context *ctx*."""


class ToolRegistry:
    """Registry for harness tools plus before/after hook extension points.

    Hooks are plain async callables. They observe (never mutate) tool
    invocations; future plugin/MCP support will attach here without any
    change to the tools themselves.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.before_hooks: list[BeforeHook] = []
        self.after_hooks: list[AfterHook] = []

    def register(self, tool: Tool) -> Tool:
        """Register *tool* under its ``name``."""
        if not tool.name or not tool.name.strip():
            raise ValueError("Tool must define a non-empty name")
        key = tool.name.strip().lower()
        if key in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[key] = tool
        log.debug("tool_registered", tool=tool.name)
        return tool

    def get(self, name: str) -> Tool:
        """Return the tool registered as *name*."""
        try:
            return self._tools[name.strip().lower()]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self) -> list[Tool]:
        """Return all registered tools in registration order."""
        return list(self._tools.values())

    def names(self) -> list[str]:
        """Return all registered tool names."""
        return [tool.name for tool in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        """Return True when a tool called *name* is registered."""
        return isinstance(name, str) and name.strip().lower() in self._tools

    def schemas(self) -> list[ToolSchema]:
        """Return LLM function-calling schemas for all tools.

        The JSON schema comes straight from each tool's pydantic v2
        ``args_schema``, so provider payloads stay in sync with validation.
        """
        return [
            ToolSchema(
                name=tool.name,
                description=tool.description,
                parameters=tool.args_schema.model_json_schema(),
            )
            for tool in self._tools.values()
        ]

    def add_before_hook(self, hook: BeforeHook) -> None:
        """Attach an observer run before each tool execution."""
        self.before_hooks.append(hook)

    def add_after_hook(self, hook: AfterHook) -> None:
        """Attach an observer run after each tool execution."""
        self.after_hooks.append(hook)

    async def run_before(self, name: str, args: BaseModel, ctx: ToolContext) -> None:
        """Run all before-hooks for a tool invocation."""
        for hook in self.before_hooks:
            await hook(name, args, ctx)

    async def run_after(
        self,
        name: str,
        args: BaseModel,
        ctx: ToolContext,
        result: ToolResult,
    ) -> None:
        """Run all after-hooks for a tool invocation."""
        for hook in self.after_hooks:
            await hook(name, args, ctx, result)

    async def execute(
        self,
        name: str,
        args: BaseModel | dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Validate *args*, run hooks, and execute the named tool."""
        tool = self.get(name)
        validated = tool.coerce_args(args)
        log.info(
            "tool_execute",
            tool=tool.name,
            session_id=ctx.session_id,
            agent=ctx.agent_name,
        )
        await self.run_before(tool.name, validated, ctx)
        result = await tool.execute(validated, ctx)
        await self.run_after(tool.name, validated, ctx, result)
        return result
