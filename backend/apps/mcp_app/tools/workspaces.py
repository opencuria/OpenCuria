"""Workspace lifecycle MCP tools.

``create_workspace`` additionally requires ``plugins:write`` for
non-empty ``plugin_ids``; ``update_workspace`` requires it whenever
``plugin_ids`` is given. Passing ``credential_ids`` requires
``credentials:read``. ``recreate_workspace`` requires
``workspaces:delete`` because the workspace data is lost.
"""

from __future__ import annotations

import asyncio
import uuid

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.credentials.services import CredentialSvc
from apps.organizations.services import OrganizationService
from apps.plugins.services import PluginService
from apps.runners.services.image_versions import ImageVersionService
from common.exceptions import ConflictError, NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="list_workspaces",
        description="List workspaces in the active organization.",
        inputSchema={
            "type": "object",
            "properties": {
                "runner_id": {
                    "type": "string",
                    "description": "Filter by runner UUID (optional).",
                }
            },
        },
    ),
    Tool(
        name="get_workspace",
        description="Get details of a single workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="create_workspace",
        description="Create a new workspace on a runner.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Workspace name."},
                "runner_id": {
                    "type": "string",
                    "description": "Runner UUID to host the workspace.",
                },
                "repos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "branch": {"type": "string"},
                        },
                        "required": ["url"],
                    },
                    "description": "Repositories to clone into the workspace.",
                },
                "image_artifact_id": {
                    "type": "string",
                    "description": "Optional image artifact UUID to start workspace from.",
                },
                "desktop_width": {
                    "type": "integer",
                    "description": "Fixed desktop width in pixels (default 1920).",
                },
                "desktop_height": {
                    "type": "integer",
                    "description": "Fixed desktop height in pixels (default 1080).",
                },
                "credential_ids": {"type": "array", "items": {"type": "string"}},
                "plugin_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="update_workspace",
        description="Update workspace metadata, including the fixed desktop size.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace UUID.",
                },
                "name": {"type": "string", "description": "New workspace name."},
                "desktop_width": {
                    "type": "integer",
                    "description": "Fixed desktop width in pixels.",
                },
                "desktop_height": {
                    "type": "integer",
                    "description": "Fixed desktop height in pixels.",
                },
                "qemu_vcpus": {"type": "integer"},
                "qemu_memory_mb": {"type": "integer"},
                "qemu_disk_size_gb": {"type": "integer"},
                "credential_ids": {"type": "array", "items": {"type": "string"}},
                "plugin_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["workspace_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="stop_workspace",
        description="Stop a running workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="resume_workspace",
        description="Resume a stopped workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="remove_workspace",
        description="Remove a workspace and its container permanently.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="recreate_workspace",
        description=(
            "Reset a workspace onto its own image version, or update it to the "
            "latest version of its image. The workspace keeps its id, chats, "
            "credentials, plugins and schedules; all data stored only inside "
            "the workspace is permanently deleted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "image_id": {
                    "type": "string",
                    "description": (
                        "Target version: base_image.id (reset) or "
                        "base_image.latest_id (update) from get_workspace."
                    ),
                },
            },
            "required": ["workspace_id", "image_id"],
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_workspaces": APIKeyPermission.WORKSPACES_READ,
    "get_workspace": APIKeyPermission.WORKSPACES_READ,
    "create_workspace": APIKeyPermission.WORKSPACES_CREATE,
    "update_workspace": APIKeyPermission.WORKSPACES_UPDATE,
    "stop_workspace": APIKeyPermission.WORKSPACES_STOP,
    "resume_workspace": APIKeyPermission.WORKSPACES_RESUME,
    "remove_workspace": APIKeyPermission.WORKSPACES_DELETE,
    "recreate_workspace": APIKeyPermission.WORKSPACES_DELETE,
}


def _call_list_workspaces(api_key, org_id, args: dict) -> list[TextContent]:
    svc = access.runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    runner_id = None
    if args.get("runner_id"):
        try:
            runner_id = uuid.UUID(args["runner_id"])
        except ValueError:
            return error_result("Invalid runner_id UUID")

    workspaces = svc.list_workspaces(
        runner_id=runner_id,
        organization_id=org_id,
        user=api_key.user,
    )
    result = [
        {
            "id": str(w.id),
            "name": w.name,
            "status": str(w.status),
            "intervention_required": w.intervention_required,
            "lifecycle_diagnostic": w.lifecycle_diagnostic,
            "runner_id": str(w.runner_id),
            "created_at": w.created_at.isoformat(),
        }
        for w in workspaces
    ]
    return text_result(result)


def _call_get_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    workspace, error = access.owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error

    versions = ImageVersionService()
    result = {
        "id": str(workspace.id),
        "name": workspace.name,
        "status": str(workspace.status),
        "intervention_required": workspace.intervention_required,
        "lifecycle_diagnostic": workspace.lifecycle_diagnostic,
        "runner_id": str(workspace.runner_id),
        "desktop_width": workspace.desktop_width,
        "desktop_height": workspace.desktop_height,
        "credential_ids": [
            str(item.id)
            for item in workspace.credentials.all().select_related("service")
            if item.service.credential_type != "provider_secret"
        ],
        "plugin_ids": [
            str(item.plugin_id) for item in workspace.plugin_activations.all()
        ],
        "base_image": versions.version_ref(workspace.base_image_instance),
        "pending_base_image": versions.version_ref(
            workspace.pending_base_image_instance
        ),
        "created_at": workspace.created_at.isoformat(),
    }
    return text_result(result)


def _call_create_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    """Synchronously dispatch workspace creation (fires and returns task info)."""
    name = args.get("name")
    if not name:
        return error_result("name is required")

    runner_id = None
    if args.get("runner_id"):
        try:
            runner_id = uuid.UUID(args["runner_id"])
        except ValueError:
            return error_result("Invalid runner_id UUID")

    repos = args.get("repos", [])
    credential_ids = args.get("credential_ids", [])
    plugin_ids = args.get("plugin_ids", [])
    if not isinstance(credential_ids, list) or not isinstance(plugin_ids, list):
        return error_result("credential_ids and plugin_ids must be arrays")
    try:
        credential_ids = [uuid.UUID(str(value)) for value in credential_ids]
        plugin_ids = [uuid.UUID(str(value)) for value in plugin_ids]
    except ValueError:
        return error_result("Invalid credential_ids or plugin_ids UUID")
    if "credential_ids" in args and not api_key.has_permission(
        APIKeyPermission.CREDENTIALS_READ
    ):
        return error_result("Permission denied: credentials:read required")
    if plugin_ids and not api_key.has_permission(APIKeyPermission.PLUGINS_WRITE):
        return error_result("Permission denied: plugins:write required")

    try:
        resolved = CredentialSvc().resolve_credentials(
            credential_ids, org_id=org_id, user=api_key.user
        )
    except Exception as exc:
        return error_result(str(exc))
    desktop_width = args.get("desktop_width")
    desktop_height = args.get("desktop_height")
    image_artifact_id = None
    if args.get("image_artifact_id"):
        try:
            image_artifact_id = uuid.UUID(args["image_artifact_id"])
        except ValueError:
            return error_result("Invalid image_artifact_id UUID")

    svc = access.runner_service()

    async def _create():
        workspace, task = await svc.create_workspace(
            name=name,
            repos=repos,
            env_vars=resolved.env_vars,
            files=resolved.files,
            ssh_keys=resolved.ssh_keys,
            credentials=resolved.credentials,
            resolved_credentials=resolved,
            runner_id=runner_id,
            image_artifact_id=image_artifact_id,
            desktop_width=desktop_width,
            desktop_height=desktop_height,
            user=api_key.user,
            organization_id=org_id,
            plugin_ids=plugin_ids,
        )
        return workspace, task

    try:
        loop = asyncio.new_event_loop()
        workspace, task = loop.run_until_complete(_create())
        loop.close()

        plugin_ids_out = [
            str(value) for value in PluginService().workspace_plugin_ids(workspace.id)
        ]
        return text_result(
            {
                "workspace_id": str(workspace.id),
                "task_id": str(task.id),
                "status": str(workspace.status),
                "intervention_required": workspace.intervention_required,
                "lifecycle_diagnostic": workspace.lifecycle_diagnostic,
                "plugin_ids": plugin_ids_out,
                "credential_sync_status": "not_required",
                "message": "Workspace creation started. Use get_workspace to check status.",
            }
        )
    except (NotFoundError, ConflictError, ValueError) as e:
        return text_result(
            {
                "error": str(e),
                "code": getattr(e, "code", "validation_error"),
                "gaps": getattr(e, "gaps", None),
            }
        )


def _call_update_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    """Update mutable workspace metadata, including desktop geometry."""
    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    _workspace, error = access.owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error

    credential_records = None
    resolved = None
    if "credential_ids" in args:
        raw_ids = args.get("credential_ids")
        if not isinstance(raw_ids, list):
            return error_result("credential_ids must be an array")
        try:
            ids = [uuid.UUID(str(value)) for value in raw_ids]
        except ValueError:
            return error_result("Invalid credential_ids UUID")
        if not api_key.has_permission(APIKeyPermission.CREDENTIALS_READ):
            return error_result("Permission denied: credentials:read required")

        try:
            resolved = CredentialSvc().resolve_credentials(
                ids, org_id=org_id, user=api_key.user
            )
            credential_records = resolved.credentials
        except Exception as exc:
            return error_result(str(exc))
    plugin_ids = None
    if "plugin_ids" in args:
        if not api_key.has_permission(APIKeyPermission.PLUGINS_WRITE):
            return error_result("Permission denied: plugins:write required")
        try:
            plugin_ids = [uuid.UUID(str(value)) for value in args["plugin_ids"]]
        except (ValueError, TypeError):
            return error_result("Invalid plugin_ids UUID")
    svc = access.runner_service()

    async def _update():
        return await svc.update_workspace(
            workspace_id,
            name=args.get("name"),
            qemu_vcpus=args.get("qemu_vcpus"),
            qemu_memory_mb=args.get("qemu_memory_mb"),
            qemu_disk_size_gb=args.get("qemu_disk_size_gb"),
            desktop_width=args.get("desktop_width"),
            desktop_height=args.get("desktop_height"),
            credentials=credential_records,
            resolved_credentials=resolved,
            plugin_ids=plugin_ids,
            user=api_key.user,
            organization_id=org_id,
        )

    try:
        loop = asyncio.new_event_loop()
        updated = loop.run_until_complete(_update())
        loop.close()
        persisted = access.runner_service().get_workspace_or_none(updated.id)
        return text_result(
            {
                "id": str(updated.id),
                "name": updated.name,
                "desktop_width": updated.desktop_width,
                "desktop_height": updated.desktop_height,
                "updated_at": updated.updated_at.isoformat(),
                "credential_ids": [
                    str(item.id) for item in persisted.credentials.all()
                ],
                "plugin_ids": [
                    str(item.plugin_id) for item in persisted.plugin_activations.all()
                ],
                "credential_sync_status": getattr(
                    updated, "credential_sync_status", "not_required"
                ),
                "credential_sync_detail": getattr(
                    updated, "credential_sync_detail", None
                ),
            }
        )
    except (NotFoundError, ConflictError, ValueError) as e:
        return text_result(
            {
                "error": str(e),
                "code": getattr(e, "code", "validation_error"),
                "gaps": getattr(e, "gaps", None),
            }
        )


def _call_stop_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    try:
        _workspace, error = access.owned_workspace_or_error(
            api_key, org_id, workspace_id
        )
        if error is not None:
            return error

        svc = access.runner_service()

        async def _stop():
            return await svc.stop_workspace(workspace_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_stop())
        loop.close()
        return text_result(
            {"task_id": str(task.id), "message": "Stop task dispatched."}
        )
    except (NotFoundError, ConflictError) as e:
        return error_result(str(e))


def _call_resume_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    try:
        _workspace, error = access.owned_workspace_or_error(
            api_key, org_id, workspace_id
        )
        if error is not None:
            return error

        svc = access.runner_service()

        async def _resume():
            return await svc.resume_workspace(workspace_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_resume())
        loop.close()
        return text_result(
            {"task_id": str(task.id), "message": "Resume task dispatched."}
        )
    except (NotFoundError, ConflictError) as e:
        return error_result(str(e))


def _call_remove_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    try:
        _workspace, error = access.owned_workspace_or_error(
            api_key, org_id, workspace_id
        )
        if error is not None:
            return error

        svc = access.runner_service()

        async def _remove():
            return await svc.remove_workspace(workspace_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_remove())
        loop.close()
        return text_result(
            {"task_id": str(task.id), "message": "Remove task dispatched."}
        )
    except (NotFoundError, ConflictError) as e:
        return error_result(str(e))


def _call_recreate_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        workspace_id = uuid.UUID(args.get("workspace_id") or "")
        image_id = uuid.UUID(args.get("image_id") or "")
    except ValueError:
        return error_result("workspace_id and image_id must be UUIDs")
    try:
        _workspace, error = access.owned_workspace_or_error(
            api_key, org_id, workspace_id
        )
        if error is not None:
            return error
        svc = access.runner_service()
        loop = asyncio.new_event_loop()
        try:
            workspace, task = loop.run_until_complete(
                svc.recreate_workspace(
                    workspace_id,
                    image_id,
                    user=api_key.user,
                    organization_id=org_id,
                )
            )
        finally:
            loop.close()
        return text_result(
            {
                "workspace_id": str(workspace.id),
                "task_id": str(task.id),
                "active_operation": workspace.active_operation,
            }
        )
    except (NotFoundError, ConflictError) as e:
        return error_result(str(e))


HANDLERS: dict[str, ToolHandler] = {
    "list_workspaces": _call_list_workspaces,
    "get_workspace": _call_get_workspace,
    "create_workspace": _call_create_workspace,
    "update_workspace": _call_update_workspace,
    "stop_workspace": _call_stop_workspace,
    "resume_workspace": _call_resume_workspace,
    "remove_workspace": _call_remove_workspace,
    "recreate_workspace": _call_recreate_workspace,
}
