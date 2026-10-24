"""Harness session and run MCP tools (create, message, steer, fork, edit,
abort, update, delete, conversations, read state).
"""

from __future__ import annotations

import asyncio
import uuid

from asgiref.sync import sync_to_async
from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="list_harness_sessions",
        description="List harness sessions for a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="create_harness_session",
        description="Create a harness session and start the first run.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "prompt": {"type": "string"},
                "agent_name": {"type": "string"},
                "harness_id": {"type": "string", "enum": ["native", "claude", "codex"]},
                "connection_id": {"type": "string", "format": "uuid"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["workspace_id", "prompt"],
        },
    ),
    Tool(
        name="send_harness_message",
        description="Send a follow-up prompt to a harness session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["session_id", "prompt"],
        },
    ),
    Tool(
        name="steer_harness_session",
        description=(
            "Send a message into the running turn of a harness session. The "
            "agent picks it up at its next step; if the turn ends first it "
            "starts a follow-up run. Fails when no run is active."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["session_id", "prompt"],
        },
    ),
    Tool(
        name="withdraw_harness_steer",
        description="Withdraw a queued steer message the agent has not picked up.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "steer_id": {"type": "string"},
            },
            "required": ["session_id", "steer_id"],
        },
    ),
    Tool(
        name="fork_harness_session",
        description=(
            "Fork a harness session, copying messages before a message by "
            "default. Set include_message to also copy that message (requires "
            "message_id); omit message_id to copy the full session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message_id": {"type": "string"},
                "include_message": {"type": "boolean", "default": False},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="edit_harness_message",
        description="Edit a user message, then drop later turns and rerun.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message_id": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["session_id", "message_id", "prompt"],
        },
    ),
    Tool(
        name="abort_harness_session",
        description="Abort the active run of a harness session.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="patch_harness_session",
        description="Rename a harness session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["session_id", "title"],
        },
    ),
    Tool(
        name="set_harness_session_mode",
        description="Switch a harness session plan|build mode (idle sessions only).",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["plan", "build"]},
            },
            "required": ["session_id", "mode"],
        },
    ),
    Tool(
        name="delete_harness_session",
        description="Delete a harness session (aborts active runs first).",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="list_harness_conversations",
        description="List harness conversations across owned workspaces in the org.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="mark_harness_session_read",
        description="Mark a harness session as read (dashboard unread state).",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="mark_harness_session_unread",
        description="Mark a harness session as unread (dashboard unread state).",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_harness_sessions": APIKeyPermission.HARNESS_READ,
    "create_harness_session": APIKeyPermission.HARNESS_RUN,
    "send_harness_message": APIKeyPermission.HARNESS_RUN,
    "steer_harness_session": APIKeyPermission.HARNESS_RUN,
    "withdraw_harness_steer": APIKeyPermission.HARNESS_RUN,
    "fork_harness_session": APIKeyPermission.HARNESS_RUN,
    "edit_harness_message": APIKeyPermission.HARNESS_RUN,
    "abort_harness_session": APIKeyPermission.HARNESS_RUN,
    "patch_harness_session": APIKeyPermission.HARNESS_RUN,
    "set_harness_session_mode": APIKeyPermission.HARNESS_RUN,
    "delete_harness_session": APIKeyPermission.HARNESS_RUN,
    "list_harness_conversations": APIKeyPermission.HARNESS_READ,
    "mark_harness_session_read": APIKeyPermission.HARNESS_READ,
    "mark_harness_session_unread": APIKeyPermission.HARNESS_READ,
}


def _session_dict(session, *, unread: bool = False) -> dict:
    """Serialize a HarnessSession ORM row for MCP responses."""
    return {
        "id": str(session.id),
        "workspace_id": str(session.workspace_id),
        "title": session.title or "",
        "mode": session.mode,
        "agent_name": session.agent_name,
        "harness_id": getattr(session, "harness_id", "native") or "native",
        "model": session.model or "",
        "reasoning_effort": getattr(session, "reasoning_effort", "") or "",
        "status": session.status,
        "cost": float(session.cost or 0.0),
        "tokens": dict(session.tokens or {}),
        "unread": unread,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _session_dict_with_unread(service, session) -> dict:
    """Serialize a session and compute its unread flag."""
    return _session_dict(session, unread=service.is_session_unread(session))


def _optional_text(value: object) -> str | None:
    """Return the stripped string or None when blank (edit no-op)."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _call_list_harness_sessions(api_key, org_id, args: dict) -> list[TextContent]:
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
    sessions = service.list_sessions(workspace_id)
    unread_map = service.unread_for_sessions(sessions)
    return text_result(
        [
            _session_dict(session, unread=unread_map.get(session.id, False))
            for session in sessions
        ]
    )


async def _call_create_harness_session(
    api_key, org_id, args: dict
) -> list[TextContent]:
    """Create a session and start the first run without nested event loops."""
    workspace_id_str = args.get("workspace_id")
    prompt = (args.get("prompt") or "").strip()
    if not workspace_id_str or not prompt:
        return error_result("workspace_id and prompt are required")
    try:
        workspace_id = uuid.UUID(workspace_id_str)
    except ValueError:
        return error_result("Invalid workspace_id UUID")

    workspace, error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, workspace_id
    )
    if error is not None:
        return error

    service = access.harness_service()

    try:
        session = await sync_to_async(service.create_session)(
            workspace_id=workspace.id,
            organization_id=org_id,
            prompt=prompt,
            harness_id=args.get("harness_id", "native"),
            connection_id=args.get("connection_id"),
            agent_name=args.get("agent_name") or "build",
            mode=args.get("mode") or "build",
            model=args.get("model") or "",
            reasoning_effort=args.get("reasoning_effort") or "",
            skill_ids=list(args.get("skill_ids") or []),
            user_id=api_key.user.id,
        )
        await service.start_run(
            session,
            prompt,
            organization_id=org_id,
            workspace_id=str(workspace.id),
            user_id=api_key.user.id,
            skill_ids=list(args.get("skill_ids") or []),
        )
        fresh = await sync_to_async(service.get_session)(session.id)
        payload = await sync_to_async(_session_dict_with_unread)(service, fresh)
        return text_result(payload)
    except AuthenticationError as exc:
        return error_result(exc.message)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return error_result(str(exc))


async def _call_send_harness_message(api_key, org_id, args: dict) -> list[TextContent]:
    """Send a follow-up prompt without nested event loops."""
    session_id_str = args.get("session_id")
    prompt = (args.get("prompt") or "").strip()
    if not session_id_str or not prompt:
        return error_result("session_id and prompt are required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = await sync_to_async(access.owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = access.harness_service()

    try:
        current = await sync_to_async(service.get_session)(session.id)
        if getattr(current, "harness_id", "native") != "native" and str(
            getattr(current, "created_by_id", "")
        ) != str(api_key.user.id):
            raise AuthenticationError(
                "Only the session owner may use its Claude connection"
            )
        await sync_to_async(service.ensure_user_promptable)(current)
        if not service.is_running(current.id):
            if args.get("mode"):
                current = await sync_to_async(service.set_mode)(
                    current.id, args["mode"]
                )
            if args.get("model"):
                current = await sync_to_async(service.set_model)(
                    current.id, args["model"]
                )
            if args.get("reasoning_effort"):
                current = await sync_to_async(service.set_reasoning_effort)(
                    current.id, args["reasoning_effort"]
                )
        await service.start_run(
            current,
            prompt,
            organization_id=org_id,
            workspace_id=str(current.workspace_id),
            user_id=api_key.user.id,
            skill_ids=list(args.get("skill_ids") or []) or None,
        )
        fresh = await sync_to_async(service.get_session)(current.id)
        payload = await sync_to_async(_session_dict_with_unread)(service, fresh)
        return text_result(payload)
    except AuthenticationError as exc:
        return error_result(exc.message)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return error_result(str(exc))


async def _call_steer_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    """Queue a message for the running turn of a session."""
    session_id_str = args.get("session_id")
    prompt = (args.get("prompt") or "").strip()
    if not session_id_str or not prompt:
        return error_result("session_id and prompt are required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = await sync_to_async(access.owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = access.harness_service()
    try:
        current = await sync_to_async(service.get_session)(session.id)
        if getattr(current, "harness_id", "native") != "native" and str(
            getattr(current, "created_by_id", "")
        ) != str(api_key.user.id):
            raise AuthenticationError(
                "Only the session owner may use its Claude connection"
            )
        entry = await service.steer_run(current, prompt, user_id=api_key.user.id)
        return text_result(entry.to_payload())
    except AuthenticationError as exc:
        return error_result(exc.message)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return error_result(str(exc))


async def _call_withdraw_harness_steer(
    api_key, org_id, args: dict
) -> list[TextContent]:
    """Withdraw a queued steer message before the run consumed it."""
    session_id_str = args.get("session_id")
    steer_id = (args.get("steer_id") or "").strip()
    if not session_id_str or not steer_id:
        return error_result("session_id and steer_id are required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = await sync_to_async(access.owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = access.harness_service()
    try:
        current = await sync_to_async(service.get_session)(session.id)
        if getattr(current, "harness_id", "native") != "native" and str(
            getattr(current, "created_by_id", "")
        ) != str(api_key.user.id):
            raise AuthenticationError(
                "Only the session owner may use its Claude connection"
            )
        entry = await service.withdraw_steer(current, steer_id)
        return text_result(entry.to_payload())
    except AuthenticationError as exc:
        return error_result(exc.message)
    except NotFoundError as exc:
        return error_result(str(exc))


async def _call_fork_harness_session(api_key, org_id, args) -> list:  # type: ignore[no-untyped-def]
    """Fork a session, excluding the cutoff message unless requested."""
    session_id_str = args.get("session_id")
    if not session_id_str:
        return error_result("session_id is required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    message_id = None
    if args.get("message_id"):
        try:
            message_id = uuid.UUID(args["message_id"])
        except ValueError:
            return error_result("Invalid message_id UUID")

    session, error = await sync_to_async(access.owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = access.harness_service()

    try:
        current = await sync_to_async(service.get_session)(session.id)
        await sync_to_async(service.ensure_user_promptable)(current)
        forked = await service.fork_session(
            current.id,
            message_id,
            include_message=args.get("include_message", False),
        )
        payload = await sync_to_async(_session_dict_with_unread)(service, forked)
        return text_result(payload)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return error_result(str(exc))


async def _call_edit_harness_message(api_key, org_id, args) -> list:  # type: ignore[no-untyped-def]
    """Edit a user message, drop the suffix, and rerun with a new prompt."""
    session_id_str = args.get("session_id")
    message_id_str = args.get("message_id")
    prompt = (args.get("prompt") or "").strip()
    if not session_id_str or not message_id_str or not prompt:
        return error_result("session_id, message_id and prompt are required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")
    try:
        message_id = uuid.UUID(message_id_str)
    except ValueError:
        return error_result("Invalid message_id UUID")

    session, error = await sync_to_async(access.owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = access.harness_service()
    mode = _optional_text(args.get("mode"))
    model = _optional_text(args.get("model"))
    effort = _optional_text(args.get("reasoning_effort"))

    try:
        current = await sync_to_async(service.get_session)(session.id)
        if getattr(current, "harness_id", "native") != "native" and str(
            getattr(current, "created_by_id", "")
        ) != str(api_key.user.id):
            raise AuthenticationError(
                "Only the session owner may use its Claude connection"
            )
        await sync_to_async(service.ensure_user_promptable)(current)
        await service.edit_user_message(
            current.id,
            message_id=message_id,
            prompt=prompt,
            mode=mode,
            model=model,
            reasoning_effort=effort,
            skill_ids=list(args.get("skill_ids") or []) or None,
            organization_id=org_id,
            workspace_id=str(current.workspace_id),
            user_id=api_key.user.id,
        )
        fresh = await sync_to_async(service.get_session)(current.id)
        payload = await sync_to_async(_session_dict_with_unread)(service, fresh)
        return text_result(payload)
    except AuthenticationError as exc:
        return error_result(exc.message)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return error_result(str(exc))


def _call_abort_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    if not session_id_str:
        return error_result("session_id is required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = access.harness_service()

    async def _abort():
        return await service.abort_run(session.id)

    loop = asyncio.new_event_loop()
    updated = loop.run_until_complete(_abort())
    loop.close()
    return text_result(_session_dict_with_unread(service, updated))


def _call_patch_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    title = (args.get("title") or "").strip()
    if not session_id_str or not title:
        return error_result("session_id and title are required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = access.harness_service()
    try:
        updated = service.update_title(session.id, title)
    except ValueError as exc:
        return error_result(str(exc))
    return text_result(_session_dict_with_unread(service, updated))


def _call_set_harness_session_mode(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    mode = (args.get("mode") or "").strip()
    if not session_id_str or not mode:
        return error_result("session_id and mode are required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = access.harness_service()
    if service.is_running(session.id):
        return error_result(f"Harness session '{session.id}' already has an active run")

    try:
        updated = service.set_mode(session.id, mode)
    except ValueError as exc:
        return error_result(str(exc))
    return text_result(_session_dict_with_unread(service, updated))


def _call_delete_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    if not session_id_str:
        return error_result("session_id is required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = access.harness_service()

    async def _delete():
        await service.delete_session(session.id)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_delete())
    loop.close()
    return text_result({"deleted": True, "session_id": str(session.id)})


def _call_list_harness_conversations(api_key, org_id, args: dict) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    runner_service = access.runner_service()
    workspaces = runner_service.list_workspaces(
        organization_id=org_id,
        user=api_key.user,
    )
    service = access.harness_service()
    workspace_ids = [workspace.id for workspace in workspaces]
    conversations = service.list_conversations(
        organization_id=org_id,
        workspace_ids=workspace_ids,
    )
    return text_result(conversations)


def _call_mark_harness_session_read(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    if not session_id_str:
        return error_result("session_id is required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = access.harness_service()
    service.mark_session_read(session.id)
    return text_result({"session_id": str(session.id), "read": True})


def _call_mark_harness_session_unread(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    if not session_id_str:
        return error_result("session_id is required")
    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        return error_result("Invalid session_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = access.harness_service()
    service.mark_session_unread(session.id)
    return text_result({"session_id": str(session.id), "unread": True})


HANDLERS: dict[str, ToolHandler] = {
    "list_harness_sessions": _call_list_harness_sessions,
    "create_harness_session": _call_create_harness_session,
    "send_harness_message": _call_send_harness_message,
    "steer_harness_session": _call_steer_harness_session,
    "withdraw_harness_steer": _call_withdraw_harness_steer,
    "fork_harness_session": _call_fork_harness_session,
    "edit_harness_message": _call_edit_harness_message,
    "abort_harness_session": _call_abort_harness_session,
    "patch_harness_session": _call_patch_harness_session,
    "set_harness_session_mode": _call_set_harness_session_mode,
    "delete_harness_session": _call_delete_harness_session,
    "list_harness_conversations": _call_list_harness_conversations,
    "mark_harness_session_read": _call_mark_harness_session_read,
    "mark_harness_session_unread": _call_mark_harness_session_unread,
}
