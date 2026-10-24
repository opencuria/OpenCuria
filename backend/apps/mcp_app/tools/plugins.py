"""Plugin catalog, org activation and workspace plugin MCP tools."""

from __future__ import annotations

import uuid

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.plugins.services import PluginService
from common.exceptions import ConflictError, NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="list_plugins",
        description=(
            "List plugins visible in the active organization (global + "
            "org-owned) with components and credential requirements "
            "(metadata only, no secrets)."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="toggle_org_plugin_activation",
        description=(
            "Enable/disable a visible plugin org-wide (admin only). "
            "Enabling requires an enabled+published definition."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plugin_id": {"type": "string", "description": "Plugin UUID."},
                "active": {"type": "boolean"},
            },
            "required": ["plugin_id", "active"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_workspace_plugins",
        description=(
            "List org-enabled plugins for a workspace with activation "
            "state and required-credential gaps (metadata only, no secrets)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
            "additionalProperties": False,
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_plugins": APIKeyPermission.PLUGINS_READ,
    "toggle_org_plugin_activation": APIKeyPermission.PLUGINS_WRITE,
    "list_workspace_plugins": APIKeyPermission.PLUGINS_READ,
}


def _plugin_payload(payload: dict) -> dict:
    """Serialize a plugin payload for MCP (metadata only, no secrets)."""
    return {
        "id": str(payload["id"]),
        "name": payload["name"],
        "slug": payload["slug"],
        "description": payload.get("description", ""),
        "enabled": bool(payload.get("enabled", True)),
        "published": bool(payload.get("published", True)),
        "organization_id": (
            str(payload["organization_id"])
            if payload.get("organization_id") is not None
            else None
        ),
        "is_global": bool(payload.get("is_global", False)),
        "org_enabled": bool(payload.get("org_enabled", False)),
        "skills": [
            {"id": str(s["id"]), "name": s["name"], "slug": s["slug"]}
            for s in payload.get("skills", [])
        ],
        "mcp_servers": [
            {
                "id": str(m["id"]),
                "name": m["name"],
                "slug": m["slug"],
                "transport": m["transport"],
            }
            for m in payload.get("mcp_servers", [])
        ],
        "credential_requirements": [
            {
                "id": str(r["id"]),
                "key": r["key"],
                "required": bool(r["required"]),
                "service_id": str(r["service_id"]),
                "service_slug": r["service_slug"],
                "service_name": r["service_name"],
                "credential_type": r["credential_type"],
            }
            for r in payload.get("credential_requirements", [])
        ],
    }


def _call_list_plugins(api_key, org_id, args: dict) -> list[TextContent]:
    OrganizationService().require_membership(api_key.user, org_id)
    payloads = PluginService().list_visible(org_id=org_id)
    return text_result([_plugin_payload(p) for p in payloads])


def _call_toggle_org_plugin_activation(
    api_key, org_id, args: dict
) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return error_result("Admin role required")
    plugin_id_str = args.get("plugin_id")
    if not plugin_id_str:
        return error_result("plugin_id is required")
    try:
        plugin_id = uuid.UUID(str(plugin_id_str))
    except ValueError:
        return error_result("Invalid plugin_id UUID")
    if not isinstance(args.get("active"), bool):
        return error_result("active must be a boolean")
    try:
        updated = PluginService().set_org_activation(
            plugin_id, org_id=org_id, user=api_key.user, active=args["active"]
        )
    except NotFoundError:
        return error_result("Plugin not found")
    except ConflictError as exc:
        return error_result(str(exc))
    return text_result(_plugin_payload(updated))


def _call_list_workspace_plugins(api_key, org_id, args: dict) -> list[TextContent]:
    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(str(workspace_id_str))
    except ValueError:
        return error_result("Invalid workspace_id UUID")
    workspace, error = access.owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error
    payloads = PluginService().list_workspace_plugins(
        workspace=workspace, org_id=org_id
    )
    return text_result(
        [
            {
                "id": str(p["id"]),
                "name": p["name"],
                "slug": p["slug"],
                "workspace_enabled": bool(p["workspace_enabled"]),
                "ready": bool(p["ready"]),
                "missing_required_credentials": [
                    {
                        "key": g["key"],
                        "service_id": str(g["service_id"]),
                        "service_name": g.get("service_name", ""),
                        "credential_type": g.get("credential_type", ""),
                        "plugin_id": str(g.get("plugin_id", p["id"])),
                        "plugin_name": g.get("plugin_name", p["name"]),
                    }
                    for g in p.get("missing_required_credentials", [])
                ],
            }
            for p in payloads
        ]
    )


HANDLERS: dict[str, ToolHandler] = {
    "list_plugins": _call_list_plugins,
    "toggle_org_plugin_activation": _call_toggle_org_plugin_activation,
    "list_workspace_plugins": _call_list_workspace_plugins,
}
