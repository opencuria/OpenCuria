"""Runner inventory, storage and lifecycle operation MCP tools."""

from __future__ import annotations

import asyncio
import uuid

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.services.disposition import DispositionService
from apps.runners.services.storage import StorageService

from .. import access
from ..results import ToolHandler, text_result

TOOLS: list[Tool] = [
    Tool(
        name="list_lifecycle_operations",
        description="List sanitized lifecycle operations visible to owner or org admin.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="inspect_lifecycle_operation",
        description="Fresh journal evidence and inventory; offline is unknown, never clears fences.",
        inputSchema={
            "type": "object",
            "properties": {"operation_id": {"type": "string"}},
            "required": ["operation_id"],
        },
    ),
    Tool(
        name="dispose_lifecycle_operation",
        description="Explicit reconcile/retry safe known failure or admin acknowledge interruption; preserves resources.",
        inputSchema={
            "type": "object",
            "properties": {
                "operation_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["reconcile", "retry", "acknowledge_interrupted"],
                },
            },
            "required": ["operation_id", "action"],
        },
    ),
    Tool(
        name="get_runner_storage",
        description="Inspect actual cached storage, generations and freshness (org admin).",
        inputSchema={
            "type": "object",
            "properties": {"runner_id": {"type": "string"}},
            "required": ["runner_id"],
        },
    ),
    Tool(
        name="refresh_runner_storage",
        description="Request a full runner storage scan (org admin).",
        inputSchema={
            "type": "object",
            "properties": {"runner_id": {"type": "string"}},
            "required": ["runner_id"],
        },
    ),
    Tool(
        name="list_runners",
        description="List runners in the active organization.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_runners": APIKeyPermission.RUNNERS_READ,
    "list_lifecycle_operations": APIKeyPermission.RUNNERS_READ,
    "inspect_lifecycle_operation": APIKeyPermission.RUNNERS_READ,
    "dispose_lifecycle_operation": APIKeyPermission.WORKSPACES_UPDATE,
    "get_runner_storage": APIKeyPermission.RUNNERS_READ,
    "refresh_runner_storage": APIKeyPermission.RUNNERS_READ,
}


def _call_list_lifecycle_operations(
    api_key, org_id: uuid.UUID, args: dict
) -> list[TextContent]:
    return text_result(
        {"operations": DispositionService.list_operations(api_key.user, org_id)}
    )


def _call_inspect_lifecycle_operation(
    api_key, org_id: uuid.UUID, args: dict
) -> list[TextContent]:
    return text_result(
        asyncio.run(
            DispositionService().inspect(
                access.runner_service(),
                api_key.user,
                org_id,
                uuid.UUID(args["operation_id"]),
            )
        )
    )


def _call_dispose_lifecycle_operation(
    api_key, org_id: uuid.UUID, args: dict
) -> list[TextContent]:
    if args["action"] == "retry":
        scope = DispositionService.retry_permission(
            api_key.user, org_id, uuid.UUID(args["operation_id"])
        )
        if scope is None or not api_key.has_permission(scope):
            raise PermissionError("Operation-specific lifecycle scope required")
    return text_result(
        asyncio.run(
            DispositionService().act(
                access.runner_service(),
                api_key.user,
                org_id,
                uuid.UUID(args["operation_id"]),
                args["action"],
            )
        )
    )


def _call_get_runner_storage(api_key, org_id, args: dict) -> list[TextContent]:
    return text_result(
        StorageService().detail(api_key.user, org_id, uuid.UUID(args["runner_id"]))
    )


def _call_refresh_runner_storage(api_key, org_id, args: dict) -> list[TextContent]:
    return text_result(
        asyncio.run(
            StorageService().refresh(
                access.runner_service(),
                api_key.user,
                org_id,
                uuid.UUID(args["runner_id"]),
            )
        )
    )


def _call_list_runners(api_key, org_id, args: dict) -> list[TextContent]:
    svc = access.runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    runners = svc.list_runners(organization_id=org_id)
    result = [
        {
            "id": str(r.id),
            "name": r.name,
            "status": str(r.status),
        }
        for r in runners
    ]
    return text_result(result)


HANDLERS: dict[str, ToolHandler] = {
    "list_runners": _call_list_runners,
    "list_lifecycle_operations": _call_list_lifecycle_operations,
    "inspect_lifecycle_operation": _call_inspect_lifecycle_operation,
    "dispose_lifecycle_operation": _call_dispose_lifecycle_operation,
    "get_runner_storage": _call_get_runner_storage,
    "refresh_runner_storage": _call_refresh_runner_storage,
}
