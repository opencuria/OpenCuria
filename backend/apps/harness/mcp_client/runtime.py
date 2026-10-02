"""Run-scoped MCP runtime: snapshot -> connections -> harness tools."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from apps.plugins import runtime as plugin_runtime
from apps.plugins.runtime_snapshot import (
    PreparedPluginRuntime,
    WorkspacePluginSnapshot,
    prepared_is_empty,
)

from ..tools.base import ToolRegistry
from .connection import (
    McpServerConnection,
    McpServerHealthError,
    McpTool,
)

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


def _resolve_pending_credentials(prepared: PreparedPluginRuntime) -> None:
    """Resolve plaintext and OAuth maps. Sync ORM; call via sync_to_async.

    Only for a runtime ``setup`` constructed itself. A prepared runtime
    from the harness is already resolved. An empty OAuth map is a valid
    result and must not be resolved again.
    """
    if prepared.workspace is None:
        return
    prepared.plaintexts.update(
        plugin_runtime.resolve_runtime_credentials(
            prepared.snapshot, workspace=prepared.workspace
        )
    )
    prepared.oauth_credentials.update(
        plugin_runtime.resolve_runtime_oauth_credentials(
            prepared.snapshot, workspace=prepared.workspace
        )
    )


class McpRuntime:
    """Owns one harness run's MCP connections (no cross-workspace pool)."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.connections: list[McpServerConnection] = []
        self.skipped: list[dict[str, str]] = []
        self._snapshot = None
        self._prepared: PreparedPluginRuntime | None = None

    @property
    def tools(self) -> list[McpTool]:
        """All discovered tools across healthy connections."""
        collected: list[McpTool] = []
        for connection in self.connections:
            if not connection.healthy:
                continue
            collected.extend(connection.harness_tools)
        return collected

    async def setup(
        self,
        *,
        workspace,
        organization_id,
        accessor: Any,
        core_tool_names: list[str] | None = None,
        snapshot: PreparedPluginRuntime | WorkspacePluginSnapshot | None = None,
        **kwargs: Any,
    ):
        """Open connections and discover tools for the effective snapshot.

        *snapshot* is a :class:`PreparedPluginRuntime` built exactly once
        by the caller (the normal run path, possibly empty) or a bare
        :class:`WorkspacePluginSnapshot` (legacy/test path: credentials
        resolve exactly once here, off the event loop). An empty OAuth
        map on a prepared runtime means resolution already ran and
        nothing matched — it is not resolved again. Extra *kwargs* are
        ignored for forward compatibility with stub runtimes. Individual
        server failures are logged as health warnings and skipped —
        including servers whose tools collide with already-kept names
        (first server wins deterministically, the colliding server is
        closed and skipped; the run itself never fails for a collision).
        """
        already_prepared = isinstance(snapshot, PreparedPluginRuntime)
        if isinstance(snapshot, PreparedPluginRuntime):
            prepared: PreparedPluginRuntime | None = snapshot
        elif isinstance(snapshot, WorkspacePluginSnapshot):
            # Bare snapshot (tests/legacy): wrap it; credentials resolve
            # exactly once below, off the event loop.
            prepared = PreparedPluginRuntime(
                snapshot=snapshot,
                workspace=workspace,
                plaintexts={},
            )
        else:
            prepared = None
        if prepared is None:
            if workspace is None:
                # No prepared snapshot and no workspace: nothing to do.
                self._snapshot = None
                return None
            snapshot_built = await sync_to_async(
                plugin_runtime.build_workspace_plugin_snapshot
            )(workspace=workspace, org_id=organization_id)
            prepared = PreparedPluginRuntime(
                snapshot=snapshot_built, workspace=workspace, plaintexts={}
            )
        self._snapshot = prepared.snapshot
        self._prepared = prepared
        if prepared_is_empty(prepared):
            return prepared.snapshot
        # Headed Playwright needs the shared desktop (KasmVNC :1) before
        # the browser spawns — otherwise it lands on no display. Ensure
        # it once per run (idempotent, best effort: a missing/foreign
        # accessor simply keeps the old headless-safe behaviour).
        await self._ensure_headed_desktop(accessor, prepared)
        if not already_prepared:
            await sync_to_async(_resolve_pending_credentials)(prepared)
        snapshot = prepared.snapshot
        await self._stack.__aenter__()
        try:
            seen: set[str] = set()
            core = {str(name).strip().lower() for name in (core_tool_names or [])}
            for plugin in snapshot.plugins:
                plaintexts = prepared.plaintexts_for(plugin.id)
                for server in plugin.mcp_servers:
                    try:
                        rendered_env, rendered_headers = (
                            plugin_runtime.render_server_config(
                                plugin, server, plaintexts
                            )
                        )
                    except plugin_runtime.PluginCredentialConfigError:
                        raise
                    except Exception as exc:
                        _record_skip(
                            self.skipped,
                            plugin=plugin.slug,
                            server=server.slug,
                            error=exc,
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
                    connection = McpServerConnection(
                        plugin_id=plugin.id,
                        plugin_slug=plugin.slug,
                        plugin_name=plugin.name,
                        server_id=server.id,
                        server_slug=server.slug,
                        server_name=server.name,
                        transport=server.transport,
                        command=(
                            [server.command, *list(server.args)]
                            if (server.transport or "").lower() == "stdio"
                            else []
                        ),
                        cwd=server.cwd or "/workspace",
                        env=rendered_env,
                        url=server.url or "",
                        headers=rendered_headers,
                        oauth_credential_id=oauth_credential_id,
                        oauth_service_id=server.oauth_service_id,
                        startup_timeout_seconds=float(
                            server.startup_timeout_seconds or 30
                        ),
                        request_timeout_seconds=float(
                            server.request_timeout_seconds or 60
                        ),
                    )
                    try:
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
                    tools = self._tools_for_connection(connection)
                    names = [tool.name for tool in tools]
                    lowered = [name.strip().lower() for name in names]
                    if any(name in core for name in lowered) or any(
                        name in seen for name in lowered
                    ):
                        # Deterministic: keep the first server that claimed
                        # a name; skip + close the colliding server.
                        connection.healthy = False
                        _record_skip(
                            self.skipped,
                            plugin=plugin.slug,
                            server=server.slug,
                            error=McpServerHealthError(
                                "MCP tool name collision; server skipped"
                            ),
                        )
                        try:
                            await connection.aclose()
                        except Exception:  # pragma: no cover - best effort
                            pass
                        continue
                    connection.harness_tools = tools
                    self.connections.append(connection)
                    self._stack.push(connection.aclose)
                    seen.update(lowered)
            if self.skipped:
                log.warning(
                    "mcp_discovery_health",
                    skipped=self.skipped,
                    servers=len(self.connections),
                )
            return snapshot
        except BaseException:
            try:
                await self._stack.aclose()
            except Exception:  # pragma: no cover - best effort
                pass
            self.connections = []
            raise

    @staticmethod
    async def _ensure_headed_desktop(
        accessor: Any, prepared: PreparedPluginRuntime
    ) -> None:
        """Start the shared desktop when a headed browser MCP needs it.

        Only servers whose env carries an explicit ``DISPLAY`` opt in —
        headless servers (no DISPLAY) keep the old behaviour. Failures
        never fail the run: the MCP open below still reports them.
        """
        try:
            servers = [
                server
                for plugin in prepared.snapshot.plugins
                for server in plugin.mcp_servers
            ]
        except Exception:  # pragma: no cover - defensive
            return
        needs_display = any(
            str((server.env or {}).get("DISPLAY", "")).strip() for server in servers
        )
        if not needs_display:
            return
        desktop_action = getattr(accessor, "desktop_action", None)
        if not callable(desktop_action):
            return
        try:
            await desktop_action("ensure", {})
        except Exception as exc:  # pragma: no cover - best effort
            log.warning(
                "mcp_headed_desktop_ensure_failed",
                error=f"{type(exc).__name__}",
            )

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
            )
            for item in connection.tools
        ]

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """Register discovered MCP tools into a run-local registry."""
        names: list[str] = []
        for connection in self.connections:
            if not connection.healthy:
                continue
            for tool in connection.harness_tools:
                registry.register(tool)
                names.append(tool.name)
        return names

    @property
    def skill_bodies(self) -> list[str]:
        """Ordered skill bodies for prompt composition (set by setup)."""
        snapshot = getattr(self, "_snapshot", None)
        if snapshot is None:
            return []
        return list(snapshot.plugin_skills)

    async def aclose(self) -> None:
        """Close all connections (idempotent, never raises).

        Plaintexts live on the :class:`PreparedPluginRuntime` owned by
        the caller (harness run scope), not on this runtime: connections
        (rendered env/headers) are closed here, and the caller's
        ``PreparedPluginRuntime`` drops out of scope with the run.
        """
        try:
            await self._stack.aclose()
        except Exception:  # pragma: no cover - best effort
            log.warning("mcp_runtime_close_failed")
        finally:
            self.connections = []
            self._snapshot = None
            self._prepared = None
