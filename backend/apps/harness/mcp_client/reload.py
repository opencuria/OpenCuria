"""Mid-run pickup of plugins enabled, disabled or edited while an agent works.

An agent can create a plugin through the OpenCuria MCP server and enable it
in its own workspace. Engines call :meth:`PluginReloader.check` between
steps: a cheap fingerprint read decides whether anything changed; only then
is the plugin runtime prepared again, new MCP servers opened, their tools
registered and the skill catalog swapped. The change is reported back to the
model as a short note so it knows which tools appeared.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import structlog
from asgiref.sync import sync_to_async

from apps.plugins.runtime import PluginCredentialConfigError
from apps.plugins.runtime_snapshot import PreparedPluginRuntime

from ..tools.base import ToolRegistry
from ..tools.skill import SkillCatalog
from .runtime import McpRuntime, RuntimeChange

log = structlog.get_logger(__name__)

#: Upper bound of tool names spelled out in one note.
_MAX_LISTED_TOOLS = 40


@dataclass
class PluginChange:
    """What changed in the workspace's plugins since the last check."""

    tools: RuntimeChange = field(default_factory=RuntimeChange)
    added_skills: list[str] = field(default_factory=list)
    removed_skills: list[str] = field(default_factory=list)
    error: str = ""

    def __bool__(self) -> bool:
        return bool(
            self.tools or self.added_skills or self.removed_skills or self.error
        )

    @property
    def tools_changed(self) -> bool:
        """Whether the tool set offered to the model changed."""
        return bool(
            self.tools.added_tools
            or self.tools.removed_tools
            or self.tools.restarted_tools
        )

    def message(self, name_of: Callable[[str], str] | None = None) -> str:
        """A note for the model; never contains secrets or server output.

        *name_of* maps registry tool names to the names the engine's model
        calls them by (Claude Code prefixes MCP server names).
        """
        rename = name_of or (lambda name: name)
        lines = ["[OpenCuria] The plugins of this workspace changed."]
        if self.error:
            lines.append(f"Plugins could not be loaded: {self.error}")
        added = [rename(name) for name in self.tools.added_tools]
        if added:
            shown = ", ".join(added[:_MAX_LISTED_TOOLS])
            more = len(added) - _MAX_LISTED_TOOLS
            lines.append(
                f"New tools you can call now: {shown}"
                + (f" (+{more} more)" if more > 0 else "")
            )
        if self.tools.removed_tools:
            lines.append(
                "Tools no longer available: "
                + ", ".join(
                    rename(name)
                    for name in self.tools.removed_tools[:_MAX_LISTED_TOOLS]
                )
            )
        if self.tools.restarted_tools:
            lines.append(
                "Updated tools (configuration changed): "
                + ", ".join(
                    rename(name)
                    for name in self.tools.restarted_tools[:_MAX_LISTED_TOOLS]
                )
            )
        for failure in self.tools.failed:
            lines.append(
                f"MCP server '{failure.get('server')}' of plugin "
                f"'{failure.get('plugin')}' did not start: {failure.get('error')}"
            )
        if self.tools.deferred:
            lines.append(
                "Changes to "
                + ", ".join(sorted(set(self.tools.deferred)))
                + " take effect with the next message."
            )
        if self.added_skills:
            lines.append(
                "New skills (load with the skill tool): " + ", ".join(self.added_skills)
            )
        if self.removed_skills:
            lines.append(
                "Skills no longer available: " + ", ".join(self.removed_skills)
            )
        return "\n".join(lines)


class PluginReloader:
    """Detects and applies plugin changes for one running agent."""

    def __init__(
        self,
        *,
        fingerprint: Callable[[], str],
        prepare: Callable[[], PreparedPluginRuntime],
        runtime: McpRuntime,
        registry: ToolRegistry,
        accessor: Any,
        catalog: SkillCatalog | None = None,
        session_id: str = "",
        turn_id: str = "",
        runtime_options: dict[str, Any] | None = None,
    ) -> None:
        self._fingerprint = fingerprint
        self._prepare = prepare
        self.runtime = runtime
        self.registry = registry
        self.accessor = accessor
        self.catalog = catalog
        self.session_id = session_id
        self.turn_id = turn_id
        self.runtime_options = runtime_options
        self._seen: str | None = None
        self._worker: asyncio.Task | None = None
        self._requests: asyncio.Queue = asyncio.Queue()

    async def prime(self) -> None:
        """Record the current fingerprint as the run's starting point."""
        self._seen = await sync_to_async(self._fingerprint)()

    async def check(self) -> PluginChange | None:
        """Apply changes since the last check; ``None`` when nothing changed.

        Runs in one dedicated task, whichever task asks: MCP connections
        nest anyio cancel scopes in the task that opens them and must close
        there (see :meth:`aclose`). Engines call this from tool handlers.
        """
        if self._worker is None:
            self._requests = asyncio.Queue()
            self._worker = asyncio.create_task(self._serve())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._requests.put_nowait(future)
        return await future

    async def aclose(self) -> None:
        """Stop the worker after it closed the connections it opened."""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        self._requests.put_nowait(None)
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.wait_for(asyncio.shield(worker), 60)
            except Exception:
                worker.cancel()
                log.warning("plugin_reload_close_failed", exc_info=True)

    async def _serve(self) -> None:
        try:
            while True:
                future = await self._requests.get()
                if future is None:
                    return
                try:
                    result = await self._apply()
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                    continue
                if not future.done():
                    future.set_result(result)
        finally:
            await self.runtime.close_children()

    async def _apply(self) -> PluginChange | None:
        try:
            current = await sync_to_async(self._fingerprint)()
        except Exception:
            log.warning("plugin_reload_fingerprint_failed", exc_info=True)
            return None
        if current == self._seen:
            return None
        self._seen = current
        try:
            prepared = await sync_to_async(self._prepare)()
        except PluginCredentialConfigError as exc:
            return PluginChange(error=str(exc))
        except Exception:
            log.warning("plugin_reload_prepare_failed", exc_info=True)
            return PluginChange(error="the plugin configuration could not be read")
        tools = await self.runtime.refresh(
            prepared=prepared,
            accessor=self.accessor,
            registry=self.registry,
            session_id=self.session_id,
            turn_id=self.turn_id,
            runtime_options=self.runtime_options,
        )
        added_skills: list[str] = []
        removed_skills: list[str] = []
        if self.catalog is not None:
            added_skills, removed_skills = self.catalog.replace(
                prepared.snapshot.skill_entries
            )
        change = PluginChange(
            tools=tools, added_skills=added_skills, removed_skills=removed_skills
        )
        return change or None
