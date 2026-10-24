"""MCP tool registry.

Each domain module declares its ``TOOLS`` (name, description, JSON input
schema), the ``PERMISSIONS`` each tool requires and the ``HANDLERS`` that
execute them. Handlers stay thin adapters over the same services as the REST
API. This package composes the domains into the registry served by
:func:`apps.mcp_app.server.create_mcp_server`.
"""

from __future__ import annotations

from mcp.types import Tool

from apps.accounts.models import APIKeyPermission

from ..results import ToolHandler
from . import (
    credentials,
    desktop,
    git,
    harness_sessions,
    harness_settings,
    harness_timeline,
    image_definitions,
    images,
    organizations,
    plugins,
    processes,
    runners,
    scheduled_tasks,
    workspaces,
)

#: Domain modules in ``tools/list`` order.
DOMAINS = (
    workspaces,
    images,
    runners,
    organizations,
    image_definitions,
    credentials,
    plugins,
    harness_settings,
    scheduled_tasks,
    harness_sessions,
    desktop,
    harness_timeline,
    processes,
    git,
)

TOOLS: list[Tool] = [tool for domain in DOMAINS for tool in domain.TOOLS]

TOOL_PERMISSIONS: dict[str, APIKeyPermission] = {
    name: permission
    for domain in DOMAINS
    for name, permission in domain.PERMISSIONS.items()
}

TOOL_HANDLERS: dict[str, ToolHandler] = {
    name: handler for domain in DOMAINS for name, handler in domain.HANDLERS.items()
}
