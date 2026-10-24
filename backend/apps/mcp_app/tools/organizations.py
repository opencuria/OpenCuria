"""Organization workspace policy MCP tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError

from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="get_workspace_policy",
        description="Get the organization's workspace policy (auto-stop, versions kept).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_workspace_policy",
        description=(
            "Update the organization's workspace policy (admin). Omitted fields "
            "stay unchanged. image_versions_to_keep: newest versions retained "
            "per image; older ones are deleted once no workspace uses them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_auto_stop_timeout_minutes": {
                    "type": ["integer", "null"],
                    "description": "Idle minutes before auto-stop; null disables.",
                },
                "image_versions_to_keep": {"type": "integer", "minimum": 1},
            },
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "get_workspace_policy": APIKeyPermission.ORGANIZATIONS_READ,
    "update_workspace_policy": APIKeyPermission.ORGANIZATIONS_WRITE,
}


def _workspace_policy_payload(org) -> dict:
    return {
        "workspace_auto_stop_timeout_minutes": org.workspace_auto_stop_timeout_minutes,
        "image_versions_to_keep": org.image_versions_to_keep,
    }


def _call_get_workspace_policy(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        org = OrganizationService().get_organization(org_id, api_key.user)
    except NotFoundError as e:
        return error_result(str(e))
    return text_result(_workspace_policy_payload(org))


def _call_update_workspace_policy(api_key, org_id, args: dict) -> list[TextContent]:
    changes = {
        key: args[key]
        for key in ("workspace_auto_stop_timeout_minutes", "image_versions_to_keep")
        if key in args
    }
    try:
        org = OrganizationService().update_workspace_policy(
            org_id=org_id, user=api_key.user, **changes
        )
    except (NotFoundError, AuthenticationError) as e:
        return error_result(str(e))
    except ValueError as e:
        return error_result(str(e))
    return text_result(_workspace_policy_payload(org))


HANDLERS: dict[str, ToolHandler] = {
    "get_workspace_policy": _call_get_workspace_policy,
    "update_workspace_policy": _call_update_workspace_policy,
}
