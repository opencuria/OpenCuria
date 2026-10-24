"""Workspace desktop MCP tools (viewer intents, clipboard, take-over).

``take_desktop_control`` requires ``harness:run`` and additionally
``terminal:access``.
"""

from __future__ import annotations

import asyncio
import uuid

from asgiref.sync import async_to_sync
from mcp.types import TextContent, Tool
from pydantic import ValidationError

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.schemas import DesktopClipboardReadIn, DesktopViewerIntentIn
from common.exceptions import ConflictError, NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    *[
        Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "format": "uuid"},
                    "viewer_client_id": {"type": "string", "format": "uuid"},
                    "intent_revision": {"type": "integer", "minimum": 1},
                },
                "required": (
                    ["workspace_id"]
                    if name == "desktop_status"
                    else ["workspace_id", "viewer_client_id", "intent_revision"]
                ),
                "additionalProperties": False,
            },
        )
        for name, description in [
            ("start_desktop", "Acquire this client's revision-fenced viewer intent."),
            (
                "stop_desktop",
                "Release this client's viewer intent, preserving other owners.",
            ),
            (
                "renew_desktop",
                "Renew an existing held viewer intent; never create one.",
            ),
            ("desktop_status", "Get desktop summaries and optional own viewer intent."),
        ]
    ],
    Tool(
        name="take_desktop_control",
        description=(
            "Abort busy computer-use agents on a workspace so a human can "
            "drive the shared desktop."
        ),
        inputSchema={
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="read_desktop_clipboard",
        description=(
            "Read text from the desktop clipboard, optionally copying the "
            "current selection first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "format": "uuid"},
                "copy_selection": {"type": "boolean", "default": False},
                "shift": {
                    "type": "boolean",
                    "default": False,
                    "description": "Hold Shift while copying the selected text.",
                },
            },
            "required": ["workspace_id"],
            "additionalProperties": False,
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "take_desktop_control": APIKeyPermission.HARNESS_RUN,
    "read_desktop_clipboard": APIKeyPermission.TERMINAL_ACCESS,
    **{
        name: APIKeyPermission.TERMINAL_ACCESS
        for name in ("start_desktop", "stop_desktop", "renew_desktop", "desktop_status")
    },
}


def _call_viewer_desktop(api_key, org_id, args: dict, action: str) -> list[TextContent]:
    """REST/MCP parity through the same authenticated viewer service methods."""
    if not api_key.has_permission(APIKeyPermission.TERMINAL_ACCESS):
        return error_result("Permission denied: terminal:access required")
    try:
        workspace_id = uuid.UUID(args["workspace_id"])
        kwargs = {"user": api_key.user, "organization_id": org_id}
        service = access.runner_service()
        if action == "status":
            client = args.get("viewer_client_id")
            return text_result(
                async_to_sync(service.viewer_desktop_status)(
                    workspace_id,
                    **kwargs,
                    viewer_client_id=uuid.UUID(client) if client else None,
                )
            )
        intent = DesktopViewerIntentIn(
            viewer_client_id=args.get("viewer_client_id"),
            intent_revision=args.get("intent_revision"),
        )
        result = async_to_sync(getattr(service, f"{action}_desktop"))(
            workspace_id,
            **kwargs,
            viewer_client_id=intent.viewer_client_id,
            intent_revision=intent.intent_revision,
        )
        return text_result(result if action == "renew" else {"task_id": str(result.id)})
    except (KeyError, ValueError, ValidationError, ConflictError, NotFoundError) as exc:
        return error_result(str(exc))


def _call_read_desktop_clipboard(api_key, org_id, args: dict) -> list[TextContent]:
    """Read clipboard text from a workspace the API key can access."""
    if not api_key.has_permission(APIKeyPermission.TERMINAL_ACCESS):
        return error_result("Permission denied: terminal:access required")

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return error_result("workspace_id is required")
    if not isinstance(workspace_id_str, str):
        return error_result("Invalid workspace_id UUID")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    if set(args) - {"workspace_id", "copy_selection", "shift"}:
        return error_result("Invalid clipboard read options")
    try:
        options = DesktopClipboardReadIn.model_validate(
            {
                "copy_selection": args.get("copy_selection", False),
                "shift": args.get("shift", False),
            }
        )
    except ValidationError:
        return error_result("Invalid clipboard read options")

    service = access.runner_service()
    organization_service = OrganizationService()
    organization_service.require_membership(api_key.user, org_id)
    if organization_service.get_user_role(api_key.user, org_id) == "admin":
        workspace = service.get_workspace(workspace_id)
        if workspace is None or workspace.runner.organization_id != org_id:
            return error_result("Workspace not found")
    else:
        workspace, error = access.owned_workspace_or_error(
            api_key, org_id, workspace_id
        )
        if error is not None:
            return error

    try:
        if options.copy_selection:
            text = async_to_sync(service.read_desktop_clipboard)(
                workspace.id,
                copy_selection=True,
                shift=options.shift,
            )
        else:
            text = async_to_sync(service.read_desktop_clipboard)(workspace.id)
    except Exception:
        # Clipboard text and runner exception details are sensitive; do not log
        # them or forward them as MCP errors.
        message = (
            "Unable to copy the desktop selection. Try again."
            if options.copy_selection
            else "Unable to read desktop clipboard"
        )
        return error_result(message)
    return text_result({"text": text})


def _call_take_desktop_control(api_key, org_id, args: dict) -> list[TextContent]:
    """Abort busy computer-use sessions so a human can drive the desktop."""
    if not api_key.has_permission(APIKeyPermission.TERMINAL_ACCESS):
        return error_result(
            f"Permission denied: {APIKeyPermission.TERMINAL_ACCESS.value} required"
        )

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

    service = access.harness_service()

    async def _abort():
        return await service.abort_busy_computeruse_for_workspace(workspace.id)

    loop = asyncio.new_event_loop()
    aborted = loop.run_until_complete(_abort())
    loop.close()
    return text_result(
        {"aborted_session_ids": [str(session.id) for session in aborted]}
    )


HANDLERS: dict[str, ToolHandler] = {
    "take_desktop_control": _call_take_desktop_control,
    "read_desktop_clipboard": _call_read_desktop_clipboard,
    **{
        name: (
            lambda api_key, org_id, args, action=action: _call_viewer_desktop(
                api_key, org_id, args, action
            )
        )
        for name, action in (
            ("start_desktop", "start"),
            ("stop_desktop", "stop"),
            ("renew_desktop", "renew"),
            ("desktop_status", "status"),
        )
    },
}
