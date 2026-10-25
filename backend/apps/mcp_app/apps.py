"""Django app config for the management MCP server."""

from __future__ import annotations

from django.apps import AppConfig


class McpAppConfig(AppConfig):
    """App config for the management MCP server (no models)."""

    name = "apps.mcp_app"
    verbose_name = "MCP server"

    def ready(self) -> None:
        """Serve the built-in OpenCuria plugin to harness runs in-process."""
        from apps.harness.mcp_client.builtin import register_builtin_server

        from .inprocess import connect

        register_builtin_server(connect)
