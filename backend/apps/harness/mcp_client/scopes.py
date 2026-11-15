"""Isolated desktop MCP lifetimes for provider-native child agent executions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import anyio
from pydantic import BaseModel

from ..desktop_leases import cleanup_rpc
from ..tools.base import Tool, ToolContext, ToolError, ToolRegistry, ToolResult


@dataclass
class DesktopScopeResources:
    """Tools available while open and recordings populated during teardown."""

    tools: ToolRegistry
    recordings: list[str] = field(default_factory=list)


DesktopScopeFactory = Callable[[], AbstractAsyncContextManager[DesktopScopeResources]]


@dataclass
class _Scope:
    ready: asyncio.Future
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    callers: set[asyncio.Task] = field(default_factory=set)


class DesktopAgentScopes:
    """Each actor owns its SDK scopes in the task that also tears them down."""

    def __init__(self, open_runtime: DesktopScopeFactory) -> None:
        """Bind a factory that owns cancellation-safe resource teardown.

        Each actor enters and exits the factory's context manager in one task;
        the factory publishes recordings on the resources before it exits.
        """
        self._open_runtime = open_runtime
        self._scopes: dict[str, _Scope] = {}
        self._ended: set[str] = set()
        self._closed = False
        self.recordings: list[str] = []

    async def tools(self, identity: str) -> ToolRegistry:
        """Prepare independent connections on first use of a child's desktop tools."""
        if self._closed or identity in self._ended:
            raise ToolError("Agent desktop scope has ended")
        scope = self._scopes.get(identity)
        if scope is None:
            if len(self._scopes) >= 128:
                raise ToolError("Native desktop agent limit reached")
            scope = _Scope(asyncio.get_running_loop().create_future())
            self._scopes[identity] = scope
            scope.task = asyncio.create_task(self._own(scope))
        elif scope.task is not None and scope.task.done():
            raise ToolError("Agent desktop runtime has ended")
        return await asyncio.shield(scope.ready)

    async def _own(self, scope: _Scope) -> None:
        resources = None
        try:
            async with self._open_runtime() as resources:
                scope.ready.set_result(resources.tools)
                try:
                    await scope.stop.wait()
                finally:
                    for caller in list(scope.callers):
                        caller.cancel()
        except BaseException as exc:
            if not scope.ready.done():
                scope.ready.set_exception(exc)
                # The only waiter may already have been cancelled.
                scope.ready.exception()
            raise
        finally:
            if resources is not None:
                self.recordings.extend(resources.recordings)

    async def execute(
        self, identity: str, name: str, args: Any, ctx: ToolContext
    ) -> ToolResult:
        """Track individual invocations so a child's exit fences its pending calls."""
        registry = await self.tools(identity)
        if identity in self._ended or self._closed:
            raise ToolError("Agent desktop scope has ended")
        scope = self._scopes[identity]
        caller = asyncio.current_task()
        if caller is not None:
            scope.callers.add(caller)
        try:
            try:
                tool = registry.get(name)
            except KeyError as exc:
                raise ToolError("Desktop tool is unavailable for this agent") from exc
            return await tool.execute(args, ctx)
        finally:
            if caller is not None:
                scope.callers.discard(caller)

    async def close(self, identity: str) -> None:
        """A native child turn ends its own claim, independently of its parent."""
        self._ended.add(identity)
        scope = self._scopes.get(identity)
        if scope is None or scope.task is None:
            return
        for caller in list(scope.callers):
            if caller is not asyncio.current_task():
                caller.cancel()
        if not scope.ready.done():
            scope.task.cancel()
        scope.stop.set()
        await cleanup_rpc(scope.task)

    async def aclose(self) -> None:
        """Fence new scopes before closing all existing child actors."""
        self._closed = True
        # A parent MCP cancel scope may already be cancelled. Start and join
        # every actor's cleanup before allowing that cancellation to propagate.
        with anyio.CancelScope(shield=True):
            results = await cleanup_rpc(
                asyncio.gather(
                    *(self.close(key) for key in self._scopes), return_exceptions=True
                )
            )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result


class ScopedDesktopTool(Tool):
    """Retain the ordinary schema/permissions while selecting trusted agent scope."""

    def __init__(self, tool: Tool, scopes: DesktopAgentScopes) -> None:
        self._tool = tool
        self.desktop_scopes = scopes
        for name in (
            "name",
            "description",
            "permission_key",
            "args_schema",
            "original_name",
            "native_mcp_server",
            "omit_tools_from",
            "execution_tool",
            "annotations",
        ):
            setattr(self, name, getattr(tool, name, None))

    def title(self, args: BaseModel) -> str:
        """Preserve the source tool's permission title."""
        return self._tool.title(args)

    def parameters_schema(self) -> dict[str, Any]:
        """Preserve original MCP schema constraints."""
        return self._tool.parameters_schema()

    async def execute(self, args: Any, ctx: ToolContext) -> ToolResult:
        """Only engines can choose a scope; it is never a model argument."""
        if ctx.agent_scope:
            return await self.desktop_scopes.execute(
                ctx.agent_scope, self.name, args, ctx
            )
        return await self._tool.execute(args, ctx)


async def close_desktop_agent_scope(registry: ToolRegistry, identity: str) -> None:
    """Notify each run-local desktop scope manager once of a native child's exit."""
    scopes = {
        tool.desktop_scopes
        for tool in registry.list()
        if isinstance(tool, ScopedDesktopTool)
    }
    for manager in scopes:
        await manager.close(identity)
