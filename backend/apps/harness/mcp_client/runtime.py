"""Run-scoped MCP runtime: snapshot -> connections -> harness tools."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

import structlog

from apps.plugins import runtime as plugin_runtime
from apps.plugins.runtime_snapshot import PreparedPluginRuntime

from ..desktop_leases import DesktopLease
from ..tools.base import ToolRegistry
from .connection import (
    McpServerConnection,
    McpServerHealthError,
    McpTool,
    check_name_collisions,
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

    async def setup(
        self,
        *,
        prepared: PreparedPluginRuntime,
        accessor: Any,
        core_tool_names: list[str] | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Open connections using the harness's already resolved credentials.

        Individual server failures are logged as health warnings and skipped —
        including servers with duplicate normalized names within their own
        discovery result or collisions with already-kept names (first server
        wins deterministically, the colliding server is closed and skipped).
        """
        snapshot = prepared.snapshot
        if not snapshot.plugins:
            return
        owner_id = owner_id or str(uuid4())
        owner_task = asyncio.current_task()
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
                    desktop = getattr(
                        getattr(server, "resources", None), "desktop", None
                    )
                    if desktop is not None:

                        def failed(conn=connection):
                            conn.healthy = False
                            if owner_task is not None:
                                owner_task.cancel()

                        connection.desktop_lease = DesktopLease(
                            accessor,
                            kind="mcp",
                            owner_id=owner_id,
                            on_failure=failed,
                        )
                        connection.desktop_activation = desktop.activation
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
                    try:
                        # Validate this server's names together before registering
                        # any of them, as well as against core + kept servers.
                        check_name_collisions(names, existing=[*core, *seen])
                    except McpServerHealthError:
                        # Deterministic: keep the first server that claimed a
                        # name; skip + close the colliding server. This also
                        # rejects aliases created by name normalization within
                        # one server, without dropping any other connection.
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
