"""Harness timeline MCP tools: timeline/part/todo reads and resolution of
pending permission and question gates.
"""

from __future__ import annotations

import asyncio
import uuid

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.harness.schemas import part_to_out, timeline_to_out
from common.exceptions import NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="get_harness_timeline",
        description="Load lightweight message/part timeline, including pending gates.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="get_harness_part",
        description="Load full detail for one part in a harness session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "part_id": {"type": "string"},
            },
            "required": ["session_id", "part_id"],
        },
    ),
    Tool(
        name="list_harness_todos",
        description="List todos of a harness session.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="resolve_harness_permission",
        description="Resolve a pending harness permission request.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "request_id": {"type": "string"},
                "response": {"type": "string"},
            },
            "required": ["session_id", "request_id", "response"],
        },
    ),
    Tool(
        name="resolve_harness_question",
        description="Answer a pending harness question request.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "question_id": {"type": "string"},
                "answers": {"type": "array", "items": {}},
                "reject": {"type": "boolean"},
            },
            "required": ["session_id", "question_id"],
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "get_harness_timeline": APIKeyPermission.HARNESS_READ,
    "get_harness_part": APIKeyPermission.HARNESS_READ,
    "list_harness_todos": APIKeyPermission.HARNESS_READ,
    "resolve_harness_permission": APIKeyPermission.HARNESS_PERMISSIONS,
    "resolve_harness_question": APIKeyPermission.HARNESS_PERMISSIONS,
}


def _is_claude_session_owner(session, user_id: object) -> bool:
    """Allow personal-engine actions only to the session's credential owner."""
    return getattr(session, "harness_id", "native") == "native" or str(
        getattr(session, "created_by_id", "")
    ) == str(user_id)


def _call_get_harness_timeline(api_key, org_id, args: dict) -> list[TextContent]:
    session_id = args.get("session_id")
    if not session_id:
        return error_result("session_id is required")
    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error
    return text_result(timeline_to_out(access.harness_service(), session))


def _call_get_harness_part(api_key, org_id, args: dict) -> list[TextContent]:
    session_id, part_id = args.get("session_id"), args.get("part_id")
    if not session_id or not part_id:
        return error_result("session_id and part_id are required")
    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error
    try:
        part_uuid = uuid.UUID(str(part_id))
    except ValueError:
        return error_result("Invalid part_id UUID")
    try:
        part = access.harness_service().get_part(session.id, part_uuid)
    except NotFoundError:
        return error_result("Harness part not found")
    return text_result(part_to_out(part))


def _call_list_harness_todos(api_key, org_id, args: dict) -> list[TextContent]:
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
    return text_result(service.list_todos(session.id))


def _call_resolve_harness_permission(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    request_id_str = args.get("request_id")
    response = (args.get("response") or "").strip()
    if not session_id_str or not request_id_str or not response:
        return error_result("session_id, request_id and response are required")
    try:
        session_id = uuid.UUID(session_id_str)
        request_id = uuid.UUID(request_id_str)
    except ValueError:
        return error_result("Invalid session_id or request_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error
    if not _is_claude_session_owner(session, api_key.user.id):
        return error_result("Only the session owner may use its Claude connection")

    service = access.harness_service()

    async def _resolve():
        return await service.resolve_permission(
            session=session,
            request_id=request_id,
            response=response,
        )

    try:
        loop = asyncio.new_event_loop()
        outcome = loop.run_until_complete(_resolve())
        loop.close()
        return text_result(
            {
                "request_id": str(request_id),
                "decision": outcome["decision"],
                "remember": outcome["remember"],
            }
        )
    except (NotFoundError, ValueError, LookupError) as exc:
        return error_result(str(exc))


def _call_resolve_harness_question(api_key, org_id, args: dict) -> list[TextContent]:
    session_id_str = args.get("session_id")
    question_id_str = args.get("question_id")
    if not session_id_str or not question_id_str:
        return error_result("session_id and question_id are required")
    try:
        session_id = uuid.UUID(session_id_str)
        question_id = uuid.UUID(question_id_str)
    except ValueError:
        return error_result("Invalid session_id or question_id UUID")

    session, error = access.owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error
    if not _is_claude_session_owner(session, api_key.user.id):
        return error_result("Only the session owner may use its Claude connection")

    answers = args.get("answers") or []
    reject = bool(args.get("reject", False))
    service = access.harness_service()

    async def _resolve():
        return await service.resolve_question(
            session=session,
            question_id=question_id,
            answers=list(answers),
            reject=reject,
        )

    try:
        loop = asyncio.new_event_loop()
        outcome = loop.run_until_complete(_resolve())
        loop.close()
        return text_result(outcome)
    except (NotFoundError, ValueError, LookupError) as exc:
        return error_result(str(exc))


HANDLERS: dict[str, ToolHandler] = {
    "get_harness_timeline": _call_get_harness_timeline,
    "get_harness_part": _call_get_harness_part,
    "list_harness_todos": _call_list_harness_todos,
    "resolve_harness_permission": _call_resolve_harness_permission,
    "resolve_harness_question": _call_resolve_harness_question,
}
