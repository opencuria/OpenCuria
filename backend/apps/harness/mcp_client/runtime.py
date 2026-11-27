"""Run-scoped MCP runtime: snapshot -> connections -> harness tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any
from uuid import uuid4

import anyio
import structlog

from apps.plugins import runtime as plugin_runtime
from apps.plugins.runtime_snapshot import (
    McpServerUnavailableError,
    PreparedPluginRuntime,
)

from ..access.environment import ExecutionEnvironmentAccessor
from ..access.guest import guest_of
from ..agent_desktop import AgentDesktop
from ..computer_use.session_host import (
    HostAttachment,
    connection_fingerprint,
    session_hosts,
)
from ..tools.base import ToolRegistry
from ..tools.desktop import DesktopTool
from .connection import (
    McpServerConnection,
    McpServerHealthError,
    McpTool,
    check_name_collisions,
)
from .scopes import DesktopAgentScopes, DesktopScopeResources, ScopedDesktopTool

log = structlog.get_logger(__name__)


def _record_skip(
    skipped: list[dict[str, str]], *, plugin: str, server: str, error: Exception
) -> None:
    """Record a server skip with a bounded, secret-free error note.

    The note keeps the exception *type* plus a short sanitized hint so
    operators can tell a runner timeout from a bad binary path without
    leaking payloads (stdout/stderr may carry secrets). The chained
    traceback stays attached via ``exc_info`` for debugging.
    """
    detail = str(error).strip().splitlines()[0][:200] if str(error).strip() else ""
    note = f"{type(error).__name__}: skipped" + (f" ({detail})" if detail else "")
    skipped.append({"plugin": plugin, "server": server, "error": note})
    log.warning(
        "mcp_server_skipped",
        plugin=plugin,
        server=server,
        error=note,
        exc_info=error,
    )


def _is_computer_use(plugin: Any, server: Any) -> bool:
    """Whether *server* is the Computer Use REPL (managed desktop kind)."""
    return server.tool_policy.get("desktop_kind") == "computeruse" or (
        plugin.slug == "computer-use" and server.slug == "cua_repl"
    )


def _discard(callbacks: list[Any], callback: Any) -> None:
    """Remove *callback* by identity if still registered."""
    for index, item in enumerate(callbacks):
        if item is callback:
            del callbacks[index]
            return


@dataclass
class _ServerEntry:
    """A guest-resolved server with its rendered configuration for one run."""

    plugin: Any
    server: Any
    env: dict[str, str] = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    oauth_credential_id: Any = field(default=None, repr=False)


@dataclass
class RuntimeChange:
    """What a mid-run :meth:`McpRuntime.refresh` changed."""

    added_tools: list[str] = field(default_factory=list)
    removed_tools: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    #: Servers that only change with the next message (managed desktop).
    deferred: list[str] = field(default_factory=list)
    #: Tools of edited servers that were reopened under the same names.
    restarted_tools: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.added_tools
            or self.removed_tools
            or self.failed
            or self.deferred
            or self.restarted_tools
        )


class McpRuntime:
    """Owns one harness run's MCP connections (no cross-workspace pool).

    The chat's Computer Use connection and desktop are borrowed from its kept
    host (:mod:`apps.harness.computer_use.session_host`) when *session_host*
    is set; the run detaches instead of closing them.
    """

    def __init__(
        self, *, scope_enabled: bool = True, session_host: bool = True
    ) -> None:
        self._stack = AsyncExitStack()
        self.connections: list[McpServerConnection] = []
        self.skipped: list[dict[str, str]] = []
        self.desktop: AgentDesktop | None = None
        self.agent_scopes: DesktopAgentScopes | None = None
        self._scope_enabled = scope_enabled
        self._session_host = session_host
        #: server ID -> version of every server this run attempted to open.
        self._known: dict[Any, str] = {}
        #: Live servers added mid-run, one child runtime each, by server ID.
        self._children: dict[Any, McpRuntime] = {}
        #: Every child in creation order (live and retired); closed in reverse.
        self._child_order: list[McpRuntime] = []

    async def execution_accessor(self, accessor: Any) -> Any:
        """Give all three engines the binding before their processes start."""
        if self.desktop is None:
            # A viewer may own the display even without any desktop plugin.
            # Binding discovery neither reserves nor activates a desktop.
            binding = await accessor.desktop_action("binding", {})
            if not binding.get("ok"):
                raise RuntimeError("Runner desktop binding is unavailable")
            environment = guest_of(accessor).desktop_environment(binding)
        else:
            environment = self.desktop.environment
        return ExecutionEnvironmentAccessor(accessor, environment)

    async def setup(
        self,
        *,
        prepared: PreparedPluginRuntime,
        accessor: Any,
        core_tool_names: list[str] | None = None,
        owner_id: str | None = None,
        session_id: str = "",
        turn_id: str = "",
        runtime_options: dict[str, Any] | None = None,
    ) -> None:
        """Open connections using the harness's already resolved credentials.

        Individual server failures are logged as health warnings and skipped —
        including servers with duplicate normalized names within their own
        discovery result or collisions with already-kept names (first server
        wins deterministically, the colliding server is closed and skipped).
        With a *session_id*, the Computer Use server and the run's desktop come
        from the chat's kept host unless another run of the chat holds it.
        """
        snapshot = prepared.snapshot
        guest = guest_of(accessor)
        self._known.update(
            {
                server.id: server.updated_at
                for plugin in snapshot.plugins
                for server in plugin.mcp_servers
            }
        )
        computer_use_declared = any(
            _is_computer_use(plugin, server)
            for plugin in snapshot.plugins
            for server in plugin.mcp_servers
            if server.resources.desktop is not None and server.runs_on(guest.name)
        )
        if self._scope_enabled:
            self.agent_scopes = DesktopAgentScopes(
                partial(
                    _open_desktop_scope,
                    prepared=_desktop_only(prepared),
                    accessor=accessor,
                    core_tool_names=core_tool_names,
                    session_id=session_id,
                    turn_id=turn_id,
                    runtime_options=runtime_options,
                )
            )
        keep = self._session_host and bool(session_id)
        if keep and not computer_use_declared:
            # Computer Use was removed from the workspace: drop the kept REPL.
            await session_hosts.close_idle(session_id, reason="server_removed")
        if not snapshot.plugins:
            return
        owner_id = owner_id or str(uuid4())
        owner_task = asyncio.current_task()
        run_context = {
            "session_id": session_id,
            "turn_id": turn_id or owner_id,
            **dict(runtime_options or {}),
        }
        await self._stack.__aenter__()
        try:
            entries = self._resolve_servers(prepared, guest.name)
            kept_entry = (
                next(
                    (
                        entry
                        for entry in entries
                        if entry.server.resources.desktop is not None
                        and _is_computer_use(entry.plugin, entry.server)
                    ),
                    None,
                )
                if keep
                else None
            )
            attachment: HostAttachment | None = None
            if kept_entry is not None:
                try:
                    attachment = await self._attach_host(
                        kept_entry,
                        accessor=accessor,
                        workspace_id=snapshot.workspace_id,
                        organization_id=snapshot.organization_id,
                        session_id=session_id,
                        recording_owner=owner_id,
                        task=owner_task,
                        run_context=run_context,
                    )
                except Exception as exc:
                    _record_skip(
                        self.skipped,
                        plugin=kept_entry.plugin.slug,
                        server=kept_entry.server.slug,
                        error=exc,
                    )
                    entries = [entry for entry in entries if entry is not kept_entry]
                if attachment is None:
                    # Busy (another run of this chat) or failed: per-run path.
                    kept_entry = None
                else:
                    self.desktop = attachment.host.desktop
                    # Detach only after every per-run connection has closed.
                    self._stack.push_async_callback(attachment.detach)
            seen: set[str] = set()
            core = {str(name).strip().lower() for name in (core_tool_names or [])}
            for entry in entries:
                plugin, server = entry.plugin, entry.server
                kept = entry is kept_entry
                if kept:
                    assert attachment is not None
                    connection = attachment.host.connection
                else:
                    connection = self._new_connection(
                        entry,
                        workspace_id=snapshot.workspace_id,
                        organization_id=snapshot.organization_id,
                        run_context=run_context,
                    )
                    desktop = getattr(
                        getattr(server, "resources", None), "desktop", None
                    )
                    if desktop is not None:
                        if self.desktop is None:
                            self.desktop = AgentDesktop(
                                accessor,
                                owner_id,
                                on_failure=(
                                    owner_task.cancel
                                    if owner_task is not None
                                    else None
                                ),
                                kind="computeruse" if computer_use_declared else "mcp",
                            )
                            # Registered before connections: release the display
                            # only after all SDK scopes and processes are closed.
                            self._stack.push_async_callback(self.desktop.aclose)
                            await self.desktop.prepare()
                        connection.agent_desktop = self.desktop
                        connection.desktop_lease = self.desktop.lease
                        connection.desktop_activation = desktop.activation
                    try:
                        if server.runtime_artifact:
                            artifact = await accessor.ensure_runtime_artifact(
                                server.runtime_artifact["id"],
                                server.runtime_artifact["version"],
                            )
                            connection.command = [artifact["path"], *list(server.args)]
                        await connection.open(accessor)
                    except plugin_runtime.PluginCredentialConfigError:
                        raise
                    except Exception as exc:
                        connection.healthy = False
                        diagnostics: dict[str, Any] = {}
                        get_diagnostics = getattr(
                            accessor, "get_stream_diagnostics", None
                        )
                        if callable(get_diagnostics):
                            try:
                                result = get_diagnostics()
                                if isinstance(result, dict):
                                    diagnostics = result
                            except Exception:  # pragma: no cover - logs only
                                diagnostics = {}
                        log.warning(
                            "mcp_server_setup_skipped",
                            plugin=plugin.slug,
                            server=server.slug,
                            transport=(server.transport or "").strip().lower(),
                            error=f"{type(exc).__name__}: "
                            f"{str(exc).strip().splitlines()[0][:200]}".strip(),
                            diagnostics=diagnostics,
                        )
                        _record_skip(
                            self.skipped,
                            plugin=plugin.slug,
                            server=server.slug,
                            error=exc,
                        )
                        try:
                            await connection.aclose()
                        except Exception:  # pragma: no cover - best effort
                            pass
                        continue
                try:
                    tools = self._tools_for_connection(connection)
                    names = [tool.name for tool in tools]
                    lowered = [name.strip().lower() for name in names]
                    # Validate this server's names together before registering
                    # any of them, as well as against core + kept servers.
                    check_name_collisions(names, existing=[*core, *seen])
                except McpServerHealthError:
                    # Deterministic: keep the first server that claimed a
                    # name; skip + close the colliding server. This also
                    # rejects aliases created by name normalization within
                    # one server, without dropping any other connection.
                    # A kept connection stays open for the chat's next run.
                    _record_skip(
                        self.skipped,
                        plugin=plugin.slug,
                        server=server.slug,
                        error=McpServerHealthError(
                            "MCP tool name collision; server skipped"
                        ),
                    )
                    if not kept:
                        connection.healthy = False
                        try:
                            await connection.aclose()
                        except Exception:  # pragma: no cover - best effort
                            pass
                    continue
                connection.harness_tools = tools
                if connection.agent_desktop is not None and not kept:
                    stop_work = connection.stop_desktop_work
                    connection.agent_desktop.before_stop.append(stop_work)
                    if attachment is not None:
                        # The kept desktop outlives this run's connections.
                        self._stack.callback(
                            _discard, connection.agent_desktop.before_stop, stop_work
                        )
                self.connections.append(connection)
                if not kept:
                    self._stack.push_async_callback(connection.aclose)
                seen.update(lowered)
            if self.skipped:
                log.warning(
                    "mcp_discovery_health",
                    skipped=self.skipped,
                    servers=len(self.connections),
                )
        except BaseException:
            try:
                await self._stack.aclose()
            except Exception:  # pragma: no cover - best effort
                pass
            self.connections = []
            raise

    def _resolve_servers(
        self, prepared: PreparedPluginRuntime, guest_name: str
    ) -> list[_ServerEntry]:
        """Resolve guest overrides and credentials; record unusable servers."""
        entries: list[_ServerEntry] = []
        for plugin in prepared.snapshot.plugins:
            plaintexts = prepared.plaintexts_for(plugin.id)
            for configured in plugin.mcp_servers:
                try:
                    server = configured.for_guest(guest_name)
                except McpServerUnavailableError as exc:
                    _record_skip(
                        self.skipped,
                        plugin=plugin.slug,
                        server=configured.slug,
                        error=exc,
                    )
                    continue
                try:
                    rendered_env, rendered_headers = (
                        plugin_runtime.render_server_config(plugin, server, plaintexts)
                    )
                except plugin_runtime.PluginCredentialConfigError:
                    raise
                except Exception as exc:
                    _record_skip(
                        self.skipped, plugin=plugin.slug, server=server.slug, error=exc
                    )
                    continue
                oauth_credential_id = prepared.oauth_credentials.get(server.id)
                if server.auth_type == "oauth" and not oauth_credential_id:
                    _record_skip(
                        self.skipped,
                        plugin=plugin.slug,
                        server=server.slug,
                        error=McpServerHealthError(
                            "MCP OAuth credential is disconnected"
                        ),
                    )
                    continue
                entries.append(
                    _ServerEntry(
                        plugin=plugin,
                        server=server,
                        env=rendered_env,
                        headers=rendered_headers,
                        oauth_credential_id=oauth_credential_id,
                    )
                )
        return entries

    @staticmethod
    def _new_connection(
        entry: _ServerEntry,
        *,
        workspace_id: Any,
        organization_id: Any,
        run_context: dict[str, Any],
    ) -> McpServerConnection:
        """Build an unopened connection for *entry*."""
        plugin, server = entry.plugin, entry.server
        return McpServerConnection(
            plugin_id=plugin.id,
            plugin_slug=plugin.slug,
            plugin_name=plugin.name,
            server_id=server.id,
            server_slug=server.slug,
            server_name=server.name,
            transport=server.transport,
            tool_policy=dict(server.tool_policy),
            run_context=dict(run_context),
            command=(
                [server.command, *list(server.args)]
                if (server.transport or "").lower() == "stdio"
                else []
            ),
            cwd=server.cwd or "/workspace",
            env=dict(entry.env),
            url=server.url or "",
            headers=dict(entry.headers),
            oauth_credential_id=entry.oauth_credential_id,
            oauth_service_id=server.oauth_service_id,
            workspace_id=workspace_id,
            organization_id=organization_id,
            startup_timeout_seconds=float(server.startup_timeout_seconds or 30),
            request_timeout_seconds=float(server.request_timeout_seconds or 60),
        )

    async def _attach_host(
        self,
        entry: _ServerEntry,
        *,
        accessor: Any,
        workspace_id: Any,
        organization_id: Any,
        session_id: str,
        recording_owner: str,
        task: asyncio.Task | None,
        run_context: dict[str, Any],
    ) -> HostAttachment | None:
        """Borrow the chat's kept Computer Use host, starting one if needed."""
        server = entry.server
        connection = self._new_connection(
            entry,
            workspace_id=workspace_id,
            organization_id=organization_id,
            run_context=run_context,
        )
        connection.desktop_activation = server.resources.desktop.activation
        fingerprint = connection_fingerprint(
            connection,
            server_version=server.updated_at,
            runtime_artifact=dict(server.runtime_artifact or {}),
        )

        async def open_connection(kept: McpServerConnection) -> None:
            if server.runtime_artifact:
                artifact = await accessor.ensure_runtime_artifact(
                    server.runtime_artifact["id"],
                    server.runtime_artifact["version"],
                )
                kept.command = [artifact["path"], *list(server.args)]
            await kept.open(accessor)
            # A broken tool contract must not be kept for later runs.
            kept.model_tools()

        return await session_hosts.attach(
            session_id=session_id,
            workspace_id=str(workspace_id),
            connection=connection,
            fingerprint=fingerprint,
            accessor=accessor,
            open_connection=open_connection,
            run_context=run_context,
            recording_owner=recording_owner,
            task=task,
        )

    def _connection_for(self, server_id: Any) -> McpServerConnection | None:
        """The live (not retired) connection of *server_id*, if any."""
        for connection in self.connections:
            if connection.server_id == server_id and connection.healthy:
                return connection
        child = self._children.get(server_id)
        return child.connections[0] if child and child.connections else None

    async def refresh(
        self,
        *,
        prepared: PreparedPluginRuntime,
        accessor: Any,
        registry: ToolRegistry,
        session_id: str = "",
        turn_id: str = "",
        runtime_options: dict[str, Any] | None = None,
    ) -> RuntimeChange:
        """Apply plugin changes made while the run is going.

        Servers that are new or changed (by ``updated_at``) are opened and
        their tools registered in *registry*; servers that disappeared are
        retired: their tools are unregistered at once, their connections
        close with the run. Connections nest anyio cancel scopes in the run's
        task and must close in reverse opening order, so none is closed out
        of order mid-run. Each server is attempted once per version, so a
        broken server is not retried every step. Managed desktop servers keep
        their state until the next message.
        """
        change = RuntimeChange()
        desired = {
            server.id: (plugin, server)
            for plugin in prepared.snapshot.plugins
            for server in plugin.mcp_servers
        }
        for server_id, version in list(self._known.items()):
            entry = desired.get(server_id)
            if entry is not None and entry[1].updated_at == version:
                continue
            connection = self._connection_for(server_id)
            if connection is not None and connection.agent_desktop is not None:
                change.deferred.append(connection.server_name)
                continue
            if connection is not None:
                for tool in connection.harness_tools:
                    if registry.unregister(tool.name) is not None:
                        change.removed_tools.append(tool.name)
                # Retired: never registered again, closed with the run.
                connection.healthy = False
            self._children.pop(server_id, None)
            del self._known[server_id]
        for server_id, (plugin, server) in desired.items():
            if server_id in self._known:
                continue
            if server.resources.desktop is not None:
                change.deferred.append(server.name)
                continue
            self._known[server_id] = server.updated_at
            child = McpRuntime(scope_enabled=False, session_host=False)
            delta = replace(
                prepared,
                snapshot=replace(
                    prepared.snapshot,
                    plugins=(replace(plugin, mcp_servers=(server,)),),
                ),
            )
            try:
                await child.setup(
                    prepared=delta,
                    accessor=accessor,
                    core_tool_names=registry.names(),
                    session_id=session_id,
                    turn_id=turn_id,
                    runtime_options=runtime_options,
                )
            except Exception as exc:
                _record_skip(
                    change.failed, plugin=plugin.slug, server=server.slug, error=exc
                )
                await child.aclose()
                continue
            change.failed.extend(child.skipped)
            if not child.connections:
                await child.aclose()
                continue
            self._children[server_id] = child
            self._child_order.append(child)
            change.added_tools.extend(child.register_tools(registry))
        # A restarted server keeps its tool names: report neither side.
        restarted = set(change.added_tools) & set(change.removed_tools)
        change.restarted_tools = [n for n in change.added_tools if n in restarted]
        change.added_tools = [n for n in change.added_tools if n not in restarted]
        change.removed_tools = [n for n in change.removed_tools if n not in restarted]
        if change:
            log.info(
                "mcp_runtime_refreshed",
                added=len(change.added_tools),
                removed=len(change.removed_tools),
                failed=change.failed,
                deferred=change.deferred,
            )
        return change

    @staticmethod
    def _tools_for_connection(connection: McpServerConnection) -> list[McpTool]:
        """Wrap discovered tools of one connection as harness tools."""
        return [
            McpTool(
                namespaced_name=item.namespaced_name,
                original_name=item.name,
                description=item.description,
                input_schema=item.input_schema,
                connection=connection,
                title_text=item.title,
                annotations=item.annotations,
            )
            for item in connection.model_tools()
        ]

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """Register discovered MCP tools into a run-local registry."""
        names: list[str] = []
        for connection in self.connections:
            if not connection.healthy:
                continue
            for tool in connection.harness_tools:
                if (
                    connection.agent_desktop is not None
                    and self.agent_scopes is not None
                ):
                    tool = ScopedDesktopTool(tool, self.agent_scopes)
                registry.register(tool)
                names.append(tool.name)
        if self.desktop is not None and any(
            connection.plugin_slug == "computer-use"
            and connection.server_slug == "cua_repl"
            and connection.healthy
            and connection.agent_desktop is self.desktop
            and (
                connection.tool_policy.get("exposed_tools") is None
                or "desktop" in connection.tool_policy["exposed_tools"]
            )
            and "desktop" not in connection.tool_policy.get("internal_tools", [])
            for connection in self.connections
        ):
            tool = DesktopTool(self.desktop)
            if tool.name in registry.names():
                raise McpServerHealthError("Desktop tool name collision")
            registry.register(
                ScopedDesktopTool(tool, self.agent_scopes)
                if self.agent_scopes is not None
                else tool
            )
            names.append(tool.name)
        return names

    async def finish(self) -> list[str]:
        """Finalize runtime effects and return only confirmed recording paths."""
        if self.agent_scopes is not None:
            await self.agent_scopes.aclose()
        for connection in self.connections:
            await connection.finish()
        for child in self._child_order:
            await child.finish()
        paths = [
            connection.recording_path
            for connection in self.connections
            if connection.recording_path
        ]
        if self.desktop is not None:
            paths.extend(await self.desktop.finish())
        if self.agent_scopes is not None:
            paths.extend(self.agent_scopes.recordings)
        return list(dict.fromkeys(paths))

    def require_computer_use(self) -> None:
        """Fail an explicitly requested specialist before calling its model."""
        if not any(
            c.plugin_slug == "computer-use" and c.healthy for c in self.connections
        ):
            raise McpServerHealthError(
                "Computer Use is unavailable. Activate the Computer Use plugin "
                "in this workspace and check its runtime health."
            )

    async def close_children(self) -> None:
        """Close servers added mid-run, newest first (in the task that opened
        them; see :class:`apps.harness.mcp_client.reload.PluginReloader`)."""
        children, self._child_order = self._child_order, []
        self._children = {}
        for child in reversed(children):
            await child.aclose()

    async def aclose(self) -> None:
        """Close all connections (idempotent, never raises).

        Plaintexts live on the :class:`PreparedPluginRuntime` owned by
        the caller (harness run scope), not on this runtime: connections
        (rendered env/headers) are closed here, and the caller's
        ``PreparedPluginRuntime`` drops out of scope with the run.
        """
        try:
            try:
                if self.agent_scopes is not None:
                    await self.agent_scopes.aclose()
            finally:
                await self.close_children()
                await self._stack.aclose()
        except Exception:  # pragma: no cover - best effort
            log.warning("mcp_runtime_close_failed")
        finally:
            self.connections = []


def _desktop_only(prepared: PreparedPluginRuntime) -> PreparedPluginRuntime:
    """Select child desktop servers while retaining the resolved credential snapshot."""
    plugins = tuple(
        replace(
            plugin,
            mcp_servers=tuple(
                server
                for server in plugin.mcp_servers
                if server.resources.desktop is not None
            ),
        )
        for plugin in prepared.snapshot.plugins
        if any(server.resources.desktop is not None for server in plugin.mcp_servers)
    )
    return replace(prepared, snapshot=replace(prepared.snapshot, plugins=plugins))


@asynccontextmanager
async def _open_desktop_scope(
    *,
    prepared: PreparedPluginRuntime,
    accessor: Any,
    core_tool_names: list[str] | None,
    session_id: str,
    turn_id: str,
    runtime_options: dict[str, Any] | None,
) -> AsyncIterator[DesktopScopeResources]:
    """Compose one child runtime and tear it down in the task that opened it.

    ``setup`` leaves the SDK sessions' cancel scopes entered in this task, and
    anyio exits scopes strictly in reverse order. Only finalization, which
    exits no scope, is shielded so that a cancelled actor still confirms its
    recordings. ``aclose`` then unwinds the SDK scopes with no scope stacked
    on top of them; the release RPCs it runs shield themselves.
    """
    runtime = McpRuntime(scope_enabled=False, session_host=False)
    resources = DesktopScopeResources(ToolRegistry())
    try:
        await runtime.setup(
            prepared=prepared,
            accessor=accessor,
            core_tool_names=core_tool_names,
            owner_id=str(uuid4()),
            session_id=session_id,
            turn_id=turn_id,
            runtime_options=runtime_options,
        )
        runtime.register_tools(resources.tools)
        yield resources
    finally:
        try:
            with anyio.CancelScope(shield=True):
                resources.recordings.extend(await runtime.finish())
        finally:
            await runtime.aclose()
