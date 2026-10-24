"""Per-connection MCP server for opencuria.

Binds the tool registry (:mod:`apps.mcp_app.tools`) to one authenticated
API key: only tools whose required permission the key holds are listed,
and every call re-checks the permission before dispatching to the
domain handler with the caller's active organization.
"""

from __future__ import annotations

import inspect
import logging
import uuid

from asgiref.sync import sync_to_async
from mcp.server import Server
from mcp.types import TextContent, Tool

from apps.organizations.services import OrganizationService

from .results import error_result
from .tools import TOOL_HANDLERS, TOOL_PERMISSIONS, TOOLS

logger = logging.getLogger(__name__)


def create_mcp_server(api_key) -> Server:
    """
    Create a per-connection MCP Server instance bound to the given API key.

    Only tools for which the API key holds the required permission are exposed.
    """
    server = Server("opencuria")

    # Filter tools to those permitted by this API key
    allowed_tools = [
        tool for tool in TOOLS if api_key.has_permission(TOOL_PERMISSIONS[tool.name])
    ]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return allowed_tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}

        # Permission check (double-check at call time)
        required_perm = TOOL_PERMISSIONS.get(name)
        if required_perm and not api_key.has_permission(required_perm):
            return error_result(f"Permission denied: {required_perm.value} required")

        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return error_result(f"Unknown tool: {name}")

        # Determine org_id from user's first org membership
        org_service = OrganizationService()
        orgs = await sync_to_async(org_service.list_user_organizations)(api_key.user)
        if not orgs:
            return error_result("User is not a member of any organization")

        org_id = uuid.UUID(str(orgs[0]["id"]))

        try:
            if inspect.iscoroutinefunction(handler):
                result = await handler(api_key, org_id, args)
            else:
                result = await sync_to_async(handler)(api_key, org_id, args)
            return result
        except Exception as exc:
            logger.exception("MCP tool %s failed", name)
            return error_result(str(exc))

    return server
