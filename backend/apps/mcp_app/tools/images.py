"""Image artifact, captured image and image deletion MCP tools.

``create_workspace_from_captured_image`` additionally requires
``plugins:write`` for non-empty ``plugin_ids`` and ``credentials:read``
for non-empty ``credential_ids``.
"""

from __future__ import annotations

import asyncio
import uuid

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.credentials.services import CredentialSvc
from apps.organizations.services import OrganizationService
from apps.runners.image_presenters import captured_image_to_out
from apps.runners.services.deletion import DeletionService
from apps.runners.services.image_versions import ImageVersionService
from common.exceptions import ConflictError, NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="preview_image_deletion",
        description="Read-only exact physical cascade preview (org admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "target_type": {
                    "type": "string",
                    "enum": ["image", "assignment", "definition", "captured_image"],
                },
                "target_id": {"type": "string"},
            },
            "required": ["target_type", "target_id"],
        },
    ),
    Tool(
        name="request_image_deletion",
        description="Retire and queue deletion; force requires org admin and preview fingerprint.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_type": {"type": "string"},
                "target_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["deferred", "force"]},
                "fingerprint": {"type": "string"},
            },
            "required": ["target_type", "target_id"],
        },
    ),
    Tool(
        name="cancel_image_deletion",
        description="Cancel before any physical deletion command is released.",
        inputSchema={
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    ),
    Tool(
        name="list_image_deletions",
        description="List durable requests or exact request status.",
        inputSchema={
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": [],
        },
    ),
    Tool(
        name="create_image_artifact",
        description=(
            "Capture a workspace as a new image (name) or as the next version of "
            "an existing captured image (captured_image_id). The workspace is "
            "stopped, captured and then recreated on the new version under the "
            "same id; it ends in its previous run state with its files intact."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "name": {"type": "string", "description": "Name of a new image."},
                "captured_image_id": {
                    "type": "string",
                    "description": "Captured image UUID to add a version to.",
                },
                "message": {
                    "type": "string",
                    "description": "Short description of what changed (optional).",
                },
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="list_captured_images",
        description=(
            "List the current user's captured images with all versions, the "
            "latest version, retention state and the workspaces using each."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_captured_image",
        description="Rename a captured image.",
        inputSchema={
            "type": "object",
            "properties": {
                "captured_image_id": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["captured_image_id", "name"],
        },
    ),
    Tool(
        name="create_workspace_from_captured_image",
        description="Create a workspace from the latest version of a captured image.",
        inputSchema={
            "type": "object",
            "properties": {
                "captured_image_id": {"type": "string"},
                "name": {"type": "string"},
                "credential_ids": {"type": "array", "items": {"type": "string"}},
                "plugin_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["captured_image_id"],
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "preview_image_deletion": APIKeyPermission.IMAGES_DELETE,
    "request_image_deletion": APIKeyPermission.IMAGES_DELETE,
    "cancel_image_deletion": APIKeyPermission.IMAGES_DELETE,
    "list_image_deletions": APIKeyPermission.IMAGES_DELETE,
    "create_image_artifact": APIKeyPermission.IMAGES_CREATE,
    "list_captured_images": APIKeyPermission.IMAGES_READ,
    "update_captured_image": APIKeyPermission.IMAGES_CREATE,
    "create_workspace_from_captured_image": APIKeyPermission.IMAGES_CLONE,
}


def _call_preview_image_deletion(api_key, org_id, args: dict) -> list[TextContent]:
    return text_result(
        DeletionService().preview(
            api_key.user, org_id, args["target_type"], uuid.UUID(args["target_id"])
        )
    )


def _call_request_image_deletion(api_key, org_id, args: dict) -> list[TextContent]:
    return text_result(
        DeletionService().request(
            api_key.user,
            org_id,
            args["target_type"],
            uuid.UUID(args["target_id"]),
            args.get("mode", "deferred"),
            args.get("fingerprint", ""),
        )
    )


def _call_cancel_image_deletion(api_key, org_id, args: dict) -> list[TextContent]:
    return text_result(
        DeletionService().cancel(api_key.user, org_id, uuid.UUID(args["request_id"]))
    )


def _call_list_image_deletions(api_key, org_id, args: dict) -> list[TextContent]:
    return text_result(
        DeletionService().list(api_key.user, org_id, args.get("request_id"))
    )


def _captured_image_payload(view: dict) -> dict:
    return captured_image_to_out(view).model_dump(mode="json")


def _call_list_captured_images(api_key, org_id, args: dict) -> list[TextContent]:
    OrganizationService().require_membership(api_key.user, org_id)
    views = ImageVersionService().list_captured_images(api_key.user, org_id)
    return text_result([_captured_image_payload(view) for view in views])


def _call_update_captured_image(api_key, org_id, args: dict) -> list[TextContent]:
    OrganizationService().require_membership(api_key.user, org_id)
    try:
        view = ImageVersionService().rename_captured_image(
            api_key.user,
            org_id,
            uuid.UUID(args.get("captured_image_id") or ""),
            args.get("name") or "",
        )
    except ValueError:
        return error_result("Invalid captured_image_id UUID")
    except (NotFoundError, ConflictError) as e:
        return error_result(str(e))
    return text_result(_captured_image_payload(view))


def _call_create_workspace_from_captured_image(
    api_key, org_id, args: dict
) -> list[TextContent]:
    OrganizationService().require_membership(api_key.user, org_id)
    plugin_ids = [uuid.UUID(p) for p in args.get("plugin_ids") or []]
    credential_ids = [uuid.UUID(c) for c in args.get("credential_ids") or []]
    if plugin_ids and not api_key.has_permission(APIKeyPermission.PLUGINS_WRITE):
        return error_result("API key lacks permission: plugins:write")
    if credential_ids and not api_key.has_permission(APIKeyPermission.CREDENTIALS_READ):
        return error_result("API key lacks permission: credentials:read")
    try:
        latest = ImageVersionService().latest_captured_version(
            api_key.user, org_id, uuid.UUID(args.get("captured_image_id") or "")
        )
        resolved = CredentialSvc().resolve_credentials(
            credential_ids, org_id=org_id, user=api_key.user
        )
        svc = access.runner_service()
        loop = asyncio.new_event_loop()
        try:
            workspace, task = loop.run_until_complete(
                svc.create_workspace_from_image_artifact(
                    image_artifact_id=latest.id,
                    name=args.get("name") or "",
                    env_vars=resolved.env_vars,
                    files=resolved.files,
                    ssh_keys=resolved.ssh_keys,
                    credentials=resolved.credentials,
                    resolved_credentials=resolved,
                    user=api_key.user,
                    organization_id=org_id,
                    plugin_ids=plugin_ids,
                )
            )
        finally:
            loop.close()
    except ValueError:
        return error_result("Invalid captured_image_id UUID")
    except (NotFoundError, ConflictError) as e:
        return error_result(str(e))
    return text_result(
        {
            "workspace_id": str(workspace.id),
            "task_id": str(task.id),
            "image_id": str(latest.id),
            "version": latest.generation,
        }
    )


def _call_create_image_artifact(api_key, org_id, args: dict) -> list[TextContent]:
    workspace_id_str = args.get("workspace_id")
    name = args.get("name") or ""
    captured_image_id = None
    if not workspace_id_str or not (name or args.get("captured_image_id")):
        return error_result(
            "workspace_id and either name or captured_image_id are required"
        )
    try:
        workspace_id = uuid.UUID(workspace_id_str)
        if args.get("captured_image_id"):
            captured_image_id = uuid.UUID(args["captured_image_id"])
    except ValueError:
        return error_result("Invalid workspace_id or captured_image_id UUID")

    try:
        workspace, error = access.owned_workspace_or_error(
            api_key, org_id, workspace_id
        )
        if error is not None:
            return error

        svc = access.runner_service()

        async def _create():
            return await svc.create_image_artifact(
                workspace_id=workspace_id,
                name=name,
                organization_id=org_id,
                captured_image_id=captured_image_id,
                message=(args.get("message") or "")[:500],
            )

        loop = asyncio.new_event_loop()
        try:
            workspace, task = loop.run_until_complete(_create())
        finally:
            loop.close()
        return text_result({"task_id": str(task.id), "workspace_id": str(workspace.id)})
    except (NotFoundError, ConflictError, ValueError) as e:
        return error_result(str(e))


HANDLERS: dict[str, ToolHandler] = {
    "preview_image_deletion": _call_preview_image_deletion,
    "request_image_deletion": _call_request_image_deletion,
    "cancel_image_deletion": _call_cancel_image_deletion,
    "list_image_deletions": _call_list_image_deletions,
    "create_image_artifact": _call_create_image_artifact,
    "list_captured_images": _call_list_captured_images,
    "update_captured_image": _call_update_captured_image,
    "create_workspace_from_captured_image": _call_create_workspace_from_captured_image,
}
