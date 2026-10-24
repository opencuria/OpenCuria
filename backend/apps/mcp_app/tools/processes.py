"""Workspace background process MCP tools."""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from common.exceptions import ConflictError, NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="list_processes",
        description="List background processes of a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="get_process",
        description="Get the status of one background process.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
    Tool(
        name="start_process",
        description="Start or restart a detached background process in a workspace (same name restarts the same app with a new log, run_count+1).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "command": {"type": "string", "description": "Shell command."},
                "workdir": {
                    "type": "string",
                    "description": "Working directory (default /workspace).",
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables.",
                },
                "name": {"type": "string", "description": "Process display name."},
            },
            "required": ["workspace_id", "command", "name"],
        },
    ),
    Tool(
        name="stop_process",
        description="Stop a background process (SIGTERM, then SIGKILL after grace).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
    Tool(
        name="restart_process",
        description="Restart a background process on the same row with a new log. Accepts UUID or name, keeps stored command/workdir.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
    Tool(
        name="delete_process",
        description="Delete a background process (stops first if running). Accepts UUID or name.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_processes": APIKeyPermission.WORKSPACES_PROCESSES_READ,
    "get_process": APIKeyPermission.WORKSPACES_PROCESSES_READ,
    "start_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "stop_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "restart_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "delete_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
}


def _process_payload(process) -> dict:
    """Serialize a WorkspaceProcess ORM row for MCP responses."""
    started_at = getattr(process, "started_at", None)
    ended_at = getattr(process, "ended_at", None)
    updated_at = getattr(process, "updated_at", None)
    session_id = getattr(process, "session_id", None)
    return {
        "id": str(getattr(process, "id", "")),
        "workspace_id": str(getattr(process, "workspace_id", "")),
        "name": getattr(process, "name", "") or "",
        "command": getattr(process, "command", "") or "",
        "workdir": getattr(process, "workdir", "") or "",
        "pid": getattr(process, "pid", None),
        "log_path": getattr(process, "log_path", "") or "",
        "status": str(getattr(process, "status", "") or ""),
        "exit_code": getattr(process, "exit_code", None),
        "run_count": getattr(process, "run_count", None),
        "kind": str(getattr(process, "kind", "") or "persistent"),
        "session_id": str(session_id) if session_id else None,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _user_visible_process_payloads(processes) -> list[dict]:
    """Filter MCP rows like the user list: persistent + running temps."""
    visible = []
    for process in processes:
        if str(getattr(process, "kind", "") or "") != "temp":
            visible.append(process)
        elif str(getattr(process, "status", "") or "") == "running":
            visible.append(process)
    return [_process_payload(process) for process in visible]


def _reject_temp_process(process, *, action: str) -> list[TextContent] | None:
    """Return an MCP error when *process* is a user-untouchable temp row."""
    if str(getattr(process, "kind", "") or "") == "temp":
        if action == "restart":
            return error_result(
                "Temporary processes cannot be restarted by users; "
                "they live only for their agent session."
            )
        return error_result(
            "Temporary processes cannot be deleted by users; stop them instead."
        )
    return None


def _parse_process_args(args: dict, *required: str) -> tuple:
    """Parse workspace/process UUIDs from MCP args or return an error."""
    values: dict[str, uuid.UUID] = {}
    for key in required:
        raw = args.get(key)
        if not raw:
            return None, error_result(f"{key} is required")
        try:
            values[key] = uuid.UUID(str(raw))
        except ValueError:
            return None, error_result(f"Invalid {key} UUID")
    return values, None


async def _call_list_processes(api_key, org_id, args: dict) -> list[TextContent]:
    """List background processes of an owned workspace (no nested loop)."""
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error

    workspace, owned_error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = access.runner_service()
    try:
        processes = await svc.list_processes(workspace.id)
    except Exception as exc:
        return error_result(str(exc))
    return text_result(_user_visible_process_payloads(processes))


async def _call_get_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Return one background process scoped to an owned workspace."""
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return error_result("process_id is required")

    workspace, owned_error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = access.runner_service()
    try:
        process = await svc.get_process(workspace.id, str(process_id))
    except NotFoundError as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    if (
        str(getattr(process, "kind", "") or "") == "temp"
        and str(getattr(process, "status", "") or "") != "running"
    ):
        return error_result("WorkspaceProcess not found")
    return text_result(_process_payload(process))


async def _call_start_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Start a detached background process in an owned workspace."""
    command = (args.get("command") or "").strip()
    name = (args.get("name") or "").strip()
    if not args.get("workspace_id"):
        return error_result("workspace_id is required")
    if not command:
        return error_result("command is required")
    if not name:
        return error_result("name is required")
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error

    workspace, owned_error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = access.runner_service()
    try:
        process = await svc.start_process(
            workspace.id,
            command,
            workdir=str(args.get("workdir") or "/workspace"),
            env=dict(args.get("env") or {}),
            name=name,
            user=api_key.user,
        )
    except (NotFoundError, ConflictError, ValueError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    return text_result(_process_payload(process))


async def _call_stop_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Stop a background process in an owned workspace."""
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return error_result("process_id is required")

    workspace, owned_error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = access.runner_service()
    try:
        process = await svc.stop_process(
            workspace.id,
            str(process_id),
        )
    except (NotFoundError, ConflictError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    return text_result(_process_payload(process))


async def _call_restart_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Restart a background process on the same row (new log).

    Temporary processes cannot be restarted by users.
    """
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return error_result("process_id is required")

    workspace, owned_error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = access.runner_service()
    try:
        existing = await svc.get_process(workspace.id, str(process_id))
    except (NotFoundError, ValueError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    rejected = _reject_temp_process(existing, action="restart")
    if rejected is not None:
        return rejected
    try:
        process = await svc.restart_process(
            workspace.id,
            str(process_id),
            user=api_key.user,
        )
    except (NotFoundError, ConflictError, ValueError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    return text_result(_process_payload(process))


async def _call_delete_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Delete a background process (stops first if running).

    Temporary processes cannot be deleted by users (stop only).
    """
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return error_result("process_id is required")

    workspace, owned_error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = access.runner_service()
    try:
        existing = await svc.get_process(workspace.id, str(process_id))
    except (NotFoundError, ValueError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    rejected = _reject_temp_process(existing, action="delete")
    if rejected is not None:
        return rejected
    try:
        deleted_id = await svc.delete_process(
            workspace.id,
            str(process_id),
        )
    except (NotFoundError, ConflictError, ValueError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
    return text_result({"deleted": True, "process_id": str(deleted_id)})


HANDLERS: dict[str, ToolHandler] = {
    "list_processes": _call_list_processes,
    "get_process": _call_get_process,
    "start_process": _call_start_process,
    "stop_process": _call_stop_process,
    "restart_process": _call_restart_process,
    "delete_process": _call_delete_process,
}
