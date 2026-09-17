"""Run-scoped MCP runtime: snapshot -> connections -> harness tools."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import structlog

from apps.plugins import runtime as plugin_runtime
from apps.plugins.runtime_snapshot import PreparedPluginRuntime, prepared_is_empty

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
        snapshot: PreparedPluginRuntime | None = None,
        **kwargs: Any,
    ):
        """Open connections and discover tools for the effective snapshot.

        *snapshot* is a :class:`PreparedPluginRuntime` built exactly once
        by the caller (the normal run path, possibly empty) or a bare
        :class:`WorkspacePluginSnapshot` (legacy/test path: credentials
        resolve exactly once here). Extra *kwargs* are ignored for
        forward compatibility with stub runtimes. Individual server
        failures are logged as health warnings and skipped — including
        servers whose tools collide with already-kept names (first
        server wins deterministically, the colliding server is closed
        and skipped; the run itself never fails for a collision).
        """
        prepared: PreparedPluginRuntime | None = snapshot
        if prepared is not None and not isinstance(prepared, PreparedPluginRuntime):
            # Bare snapshot (tests/legacy): wrap it; credentials resolve
            # exactly once below.
            prepared = PreparedPluginRuntime(
                snapshot=prepared,
                workspace=workspace,
                plaintexts={},  # type: ignore[arg-type]
            )
        if prepared is None:
            if workspace is None:
                # No prepared snapshot and no workspace: nothing to do.
                self._snapshot = None
                return None
            snapshot_built = plugin_runtime.build_workspace_plugin_snapshot(
                workspace=workspace, org_id=organization_id
            )
            prepared = PreparedPluginRuntime(
                snapshot=snapshot_built, workspace=workspace, plaintexts={}
            )
        self._snapshot = prepared.snapshot
        self._prepared = prepared
        if prepared_is_empty(prepared):
            return prepared.snapshot
        if not prepared.plaintexts:
            prepared.plaintexts.update(
                plugin_runtime.resolve_runtime_credentials(
                    prepared.snapshot, workspace=prepared.workspace
                )
            )
        resolved = prepared.plaintexts
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
