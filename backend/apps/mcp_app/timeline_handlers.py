"""MCP handlers for loading lightweight timelines and expanded part details."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.types import TextContent


def get_harness_timeline(api_key: Any, org_id: Any, args: dict) -> list[TextContent]:
    """Return the lightweight REST timeline envelope through MCP."""
    from apps.harness.api import _session_to_out_with_unread
    from apps.harness.timeline import (
        project_question_definitions,
        timeline_part_payload,
    )
    from apps.mcp_app.server import (
        _error,
        _get_harness_service,
        _owned_harness_session_or_error,
        _serialise,
        _text,
    )

    session_id = args.get("session_id")
    if not session_id:
        return _error("session_id is required")
    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error
    service = _get_harness_service()
    messages = service.list_timeline_messages(session.id)
    parts = service.list_timeline_parts(session.id)
    positions = {str(message.id): int(message.position or 0) for message in messages}
    by_message: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        part.message_position = positions.get(str(part.message_id), 0)
        payload = timeline_part_payload(
            part,
            text_output=(
                getattr(part, "timeline_output", "") if part.type == "text" else None
            ),
        )
        by_message.setdefault(str(part.message_id), []).append(_serialise(payload))
    pending_questions = service.list_pending_questions(
        session.id, include_descendants=True
    )
    pending_questions = [
        {
            **question,
            "questions": project_question_definitions(question.get("questions")),
        }
        for question in pending_questions
    ]
    return _text(
        {
            "session": _session_to_out_with_unread(service, session).model_dump(
                mode="json"
            ),
            "messages": [
                {
                    "id": str(message.id),
                    "role": message.role,
                    "content": (message.timeline_content or ""),
                    "model": message.model or "",
                    "reasoning_effort": message.reasoning_effort or "",
                    "cost": float(message.cost or 0.0),
                    "tokens": dict(message.tokens or {}),
                    "finish": message.finish or "",
                    "error": message.error or "",
                    "skill_ids": [str(value) for value in (message.skill_ids or [])],
                    "notice_dismissed_at": (
                        message.notice_dismissed_at.isoformat()
                        if message.notice_dismissed_at
                        else None
                    ),
                    "position": int(message.position or 0),
                    "message_position": int(message.position or 0),
                    "created_at": message.created_at.isoformat(),
                    "completed_at": (
                        message.completed_at.isoformat()
                        if message.completed_at
                        else None
                    ),
                    "parts": by_message.get(str(message.id), []),
                }
                for message in messages
            ],
            "permissions": service.list_pending_permissions(
                session.id, include_descendants=True
            ),
            "questions": pending_questions,
        }
    )


def get_harness_part(api_key: Any, org_id: Any, args: dict) -> list[TextContent]:
    """Return one full part after verifying ownership of both IDs."""
    from apps.mcp_app.server import (
        _error,
        _get_harness_service,
        _owned_harness_session_or_error,
        _text,
    )
    from common.exceptions import NotFoundError

    session_id, part_id = args.get("session_id"), args.get("part_id")
    if not session_id or not part_id:
        return _error("session_id and part_id are required")
    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error
    try:
        part_uuid = uuid.UUID(str(part_id))
    except ValueError:
        return _error("Invalid part_id UUID")
    service = _get_harness_service()
    try:
        part = service.get_part(session.id, part_uuid)
    except NotFoundError:
        return _error("Harness part not found")
    meta = dict(part.meta or {})
    if part.type == "patch":
        meta.pop("old_content", None)
        meta.pop("new_content", None)
    return _text(
        {
            "id": str(part.id),
            "message_id": str(part.message_id),
            "type": part.type,
            "state": part.state,
            "call_id": part.call_id or "",
            "title": part.title or "",
            "output": part.output or "",
            "input": dict(part.input or {}),
            "meta": meta,
            "position": int(part.position or 0),
            "message_position": int(getattr(part, "message_position", 0) or 0),
            "display": {
                key: value
                for key, value in dict(part.display or {}).items()
                if key != "_reasoning_line"
            },
            "detail_loaded": True,
        }
    )
