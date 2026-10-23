"""
REST API for harness sessions.

Thin Ninja routers: validate input, resolve organization + owner
scoping via the runners ``RunnerService`` (``get_workspace_for_user``
equivalent), delegate to ``HarnessService``, and format responses.
No business logic here.

Fine-grained API key permissions (new keys, never auto-granted to
existing keys — see ``APIKeyPermission``)::

- ``harness:read`` — list/get sessions, messages/parts, todos, provider config
- ``harness:run`` — create sessions, send follow-up prompts, abort,
  switch session mode, rename/delete sessions, save/delete provider config
- ``harness:permissions`` — resolve permission and question requests
- ``harness:providers`` — manage named provider accounts and ChatGPT OAuth
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.services import get_runner_service
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .schemas import (
    AgentConfigOut,
    AgentConfigSaveIn,
    AgentSConfigIn,
    AgentSConfigOut,
    ChatGPTOAuthCancelIn,
    ChatGPTOAuthStartIn,
    ChatGPTOAuthStartOut,
    ChatGPTOAuthStatusOut,
    HarnessConversationOut,
    HarnessForkIn,
    HarnessMessageEditIn,
    HarnessMessageIn,
    HarnessModeIn,
    HarnessPermissionOut,
    HarnessPermissionResolveIn,
    HarnessQuestionOut,
    HarnessQuestionResolveIn,
    HarnessSessionCreateIn,
    HarnessSessionOut,
    HarnessSessionPatchIn,
    HarnessSteerIn,
    HarnessSteerOut,
    HarnessTodoOut,
    ProviderAccountIn,
    ProviderAccountOut,
    ProviderConfigIn,
    ProviderConfigOut,
    ProviderSelectionIn,
    RecentModelOut,
    RecentModelSaveIn,
    SubagentConfigIn,
    SubagentConfigOut,
    agent_s_config_to_out,
    part_to_out,
    provider_config_to_out,
    session_to_out,
    session_to_out_with_unread,
    timeline_to_out,
)

harness_router = Router(tags=["harness"])


def _perm_denied(permission: APIKeyPermission):  # type: ignore[no-untyped-def]
    """Return a 403 error tuple for a denied API key permission."""
    return 403, {
        "detail": f"API key lacks permission: {permission.value}",
        "code": "permission_denied",
    }


def _get_service():  # type: ignore[no-untyped-def]
    """Return the runners service (owner scoping lives there)."""
    return get_runner_service()


def _resolve_harness_service():  # type: ignore[no-untyped-def]
    """Return the process-wide HarnessService (runner accessor wired)."""
    from apps.harness.harness_service import get_harness_service

    return get_harness_service()


def _get_org_id(request: HttpRequest) -> uuid.UUID:
    """Extract the organization ID from the X-Organization-Id header."""
    org_id_str = request.headers.get("X-Organization-Id")
    if not org_id_str:
        raise AuthenticationError("X-Organization-Id header is required")
    try:
        return uuid.UUID(org_id_str)
    except ValueError:
        raise AuthenticationError("Invalid X-Organization-Id header")


def _non_empty(value: str) -> str | None:
    """Return the stripped string or None when blank (edit no-op)."""
    stripped = (value or "").strip()
    return stripped or None


def _require_claude_session_owner(session, user_id: object) -> None:
    """Prevent another workspace collaborator from using personal Claude auth."""
    if getattr(session, "harness_id", "native") != "native" and str(
        getattr(session, "created_by_id", "")
    ) != str(user_id):
        raise AuthenticationError(
            "Only the session owner may use its external engine connection"
        )


def _owned_workspace(request: HttpRequest, org_id: uuid.UUID, workspace_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Return a workspace only for org members + owners (404 otherwise)."""
    service = _get_service()
    try:
        return service.get_workspace_for_user(
            workspace_id,
            user=request.user,
            organization_id=org_id,
        )
    except NotFoundError:
        raise NotFoundError("Workspace", str(workspace_id))


def _agent_config_to_out(row: dict) -> AgentConfigOut:
    """Map an AgentConfigService row to AgentConfigOut."""
    return AgentConfigOut(
        agent=row["agent"],
        mode=row["mode"],
        description=row["description"],
        model=row.get("model") or "",
        effort=row.get("effort") or "",
        inherit_model=bool(row.get("inherit_model", False)),
        effort_strategy=row.get("effort_strategy") or "fixed",
    )


def _recent_model_to_out(row) -> RecentModelOut:  # type: ignore[no-untyped-def]
    """Map a RecentModel ORM row to its response schema."""
    return RecentModelOut(
        model=row.model or "",
        effort=row.effort or "",
        last_used_at=row.last_used_at,
    )


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@harness_router.get(
    "/workspaces/{workspace_id}/harness/sessions/",
    response={200: list[HarnessSessionOut], 403: dict, 404: dict},
    summary="List harness sessions for a workspace",
)
def list_harness_sessions(request: HttpRequest, workspace_id: uuid.UUID):
    """Return all harness sessions of a workspace (owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        _owned_workspace(request, org_id, workspace_id)
        service = _resolve_harness_service()
        sessions = service.list_sessions(workspace_id)
        unread_map = service.unread_for_sessions(sessions)
        attention_map = service.attention_for_sessions(sessions)
        return 200, [
            session_to_out(
                session,
                unread=unread_map.get(session.id, False),
                manual_unread=session.manual_unread_at is not None,
                needs_attention=attention_map.get(session.id, {}).get(
                    "needs_attention", False
                ),
                attention_kind=attention_map.get(session.id, {}).get(
                    "attention_kind", ""
                ),
            )
            for session in sessions
        ]
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.post(
    "/workspaces/{workspace_id}/harness/sessions/",
    response={
        201: HarnessSessionOut,
        400: dict,
        403: dict,
        404: dict,
        409: dict,
    },
    summary="Create a harness session and start the first run",
)
async def create_harness_session(
    request: HttpRequest, workspace_id: uuid.UUID, payload: HarnessSessionCreateIn
):
    """Create a session and dispatch the first prompt as a background run."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        workspace = await sync_to_async(_owned_workspace)(request, org_id, workspace_id)
        service = _resolve_harness_service()
        session = await sync_to_async(service.create_session)(
            workspace_id=workspace.id,
            organization_id=org_id,
            prompt=payload.prompt,
            agent_name=payload.agent_name or "build",
            harness_id=payload.harness_id,
            connection_id=payload.connection_id,
            mode=payload.mode or "build",
            model=payload.model or "",
            reasoning_effort=payload.reasoning_effort or "",
            skill_ids=payload.skill_ids or [],
            user_id=request.user.id,
        )
        await service.start_run(
            session,
            payload.prompt,
            organization_id=org_id,
            workspace_id=str(workspace.id),
            user_id=request.user.id,
            skill_ids=payload.skill_ids or [],
        )
        fresh = await sync_to_async(service.get_session)(session.id)
        return 201, await sync_to_async(session_to_out_with_unread)(service, fresh)
    except AuthenticationError as exc:
        status = 403 if exc.code == "authentication_error" else 401
        return status, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/message",
    response={
        202: HarnessSessionOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        409: dict,
    },
    summary="Send a follow-up prompt to a harness session",
)
async def send_harness_message(
    request: HttpRequest, session_id: uuid.UUID, payload: HarnessMessageIn
):
    """Start a follow-up run on an idle session (409 when busy)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        try:
            _require_claude_session_owner(session, request.user.id)
        except AuthenticationError as exc:
            return 403, {"detail": exc.message, "code": "permission_denied"}
        await sync_to_async(service.ensure_user_promptable)(session)
        if not service.is_running(session.id):
            if payload.mode and payload.mode.strip():
                session = await sync_to_async(service.set_mode)(
                    session.id, payload.mode
                )
            if payload.model and payload.model.strip():
                session = await sync_to_async(service.set_model)(
                    session.id, payload.model
                )
            if payload.reasoning_effort and payload.reasoning_effort.strip():
                session = await sync_to_async(service.set_reasoning_effort)(
                    session.id, payload.reasoning_effort
                )
        await service.start_run(
            session,
            payload.prompt,
            organization_id=org_id,
            workspace_id=str(session.workspace_id),
            user_id=request.user.id,
            skill_ids=payload.skill_ids if payload.skill_ids else None,
        )
        fresh = await sync_to_async(service.get_session)(session.id)
        return 202, await sync_to_async(session_to_out_with_unread)(service, fresh)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


def _steer_to_out(entry) -> HarnessSteerOut:  # type: ignore[no-untyped-def]
    """Map a :class:`SteerEntry` to its response schema."""
    return HarnessSteerOut(
        id=uuid.UUID(entry.id), content=entry.content, created_at=entry.created_at
    )


@harness_router.post(
    "/harness/sessions/{session_id}/steer",
    response={202: HarnessSteerOut, 400: dict, 403: dict, 404: dict, 409: dict},
    summary="Send a message into the running turn of a harness session",
)
async def steer_harness_session(
    request: HttpRequest, session_id: uuid.UUID, payload: HarnessSteerIn
):
    """Queue a message the active run picks up at its next step boundary.

    Answers 409 ``no_active_run`` when the run already ended; clients then
    send the message as a regular prompt. A message still queued when the
    run settles automatically starts a follow-up run.
    """
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    await sync_to_async(OrganizationService().require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        _require_claude_session_owner(session, request.user.id)
        entry = await service.steer_run(
            session, payload.prompt, user_id=request.user.id
        )
        return 202, _steer_to_out(entry)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/harness/sessions/{session_id}/steer/{steer_id}",
    response={200: HarnessSteerOut, 403: dict, 404: dict},
    summary="Withdraw a queued message before the running turn picked it up",
)
async def withdraw_harness_steer(
    request: HttpRequest, session_id: uuid.UUID, steer_id: uuid.UUID
):
    """Remove a queued message; 404 ``not_found`` once the run consumed it."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    await sync_to_async(OrganizationService().require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        _require_claude_session_owner(session, request.user.id)
        entry = await service.withdraw_steer(session, str(steer_id))
        return 200, _steer_to_out(entry)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.post(
    "/harness/sessions/{session_id}/fork",
    response={201: HarnessSessionOut, 400: dict, 403: dict, 404: dict},
    summary="Fork a harness session (read-only, works while busy)",
)
async def fork_harness_session(
    request: HttpRequest, session_id: uuid.UUID, payload: HarnessForkIn
):
    """Fork a session, excluding the cutoff message unless requested."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        forked = await service.fork_session(
            session.id,
            payload.message_id,
            include_message=payload.include_message,
        )
        out = await sync_to_async(session_to_out_with_unread)(service, forked)
        return 201, out
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/messages/{message_id}/edit",
    response={
        202: HarnessSessionOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        409: dict,
    },
    summary="Edit a user message and rerun the session from there",
)
async def edit_harness_message(
    request: HttpRequest,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: HarnessMessageEditIn,
):
    """Edit a user message, drop the suffix, and start a rerun (409 busy)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        try:
            _require_claude_session_owner(session, request.user.id)
        except AuthenticationError as exc:
            return 403, {"detail": exc.message, "code": "permission_denied"}
        await service.edit_user_message(
            session.id,
            message_id=message_id,
            prompt=payload.prompt,
            mode=_non_empty(payload.mode),
            model=_non_empty(payload.model),
            reasoning_effort=_non_empty(payload.reasoning_effort),
            skill_ids=payload.skill_ids if payload.skill_ids else None,
            organization_id=org_id,
            workspace_id=str(session.workspace_id),
            user_id=request.user.id,
        )
        fresh = await sync_to_async(service.get_session)(session.id)
        return 202, await sync_to_async(session_to_out_with_unread)(service, fresh)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.patch(
    "/harness/sessions/{session_id}",
    response={200: HarnessSessionOut, 400: dict, 403: dict, 404: dict},
    summary="Update a harness session (title)",
)
async def patch_harness_session(
    request: HttpRequest, session_id: uuid.UUID, payload: HarnessSessionPatchIn
):
    """Rename a harness session (owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        if not (payload.title or "").strip():
            raise ValueError("title must not be empty")
        updated = await sync_to_async(service.update_title)(session.id, payload.title)
        return 200, await sync_to_async(session_to_out_with_unread)(service, updated)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/harness/sessions/{session_id}",
    response={204: None, 403: dict, 404: dict, 409: dict},
    summary="Delete a harness session",
)
async def delete_harness_session(request: HttpRequest, session_id: uuid.UUID):
    """Delete a session, aborting any active run first (owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        await service.delete_session(session.id)
        return 204, None
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/harness/conversations/",
    response={200: list[HarnessConversationOut], 403: dict},
    summary="List harness conversations across owned workspaces",
)
def list_harness_conversations(request: HttpRequest):
    """Return root sessions across the caller's workspaces in the org."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    runner_service = _get_service()
    workspaces = runner_service.list_workspaces(
        organization_id=org_id,
        user=request.user,
    )
    service = _resolve_harness_service()
    workspace_ids = [workspace.id for workspace in workspaces]
    rows = service.list_conversations(
        organization_id=org_id,
        workspace_ids=workspace_ids,
    )
    # ``list_conversations`` predates engine selection; resolve the public
    # engine identifier separately rather than exposing the private connection.
    harness_ids = {
        str(session.id): getattr(session, "harness_id", "native") or "native"
        for workspace_id in workspace_ids
        for session in service.list_sessions(workspace_id)
    }
    return 200, [
        HarnessConversationOut(
            session_id=uuid.UUID(row["session_id"]),
            workspace_id=uuid.UUID(row["workspace_id"]),
            workspace_name=row["workspace_name"],
            title=row["title"],
            status=row["status"],
            mode=row["mode"],
            agent_name=row["agent_name"],
            harness_id=harness_ids.get(str(row["session_id"]), "native"),
            model=row.get("model") or "",
            reasoning_effort=row.get("reasoning_effort") or "",
            unread=row["unread"],
            manual_unread=bool(row.get("manual_unread")),
            needs_attention=bool(row.get("needs_attention")),
            attention_kind=row.get("attention_kind") or "",
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
        )
        for row in rows
    ]


@harness_router.post(
    "/harness/sessions/{session_id}/read",
    response={204: None, 403: dict, 404: dict},
    summary="Mark a harness session as read",
)
def mark_harness_session_read(request: HttpRequest, session_id: uuid.UUID):
    """Record that the user opened a harness session (dashboard unread)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        service.mark_session_read(session.id)
        return 204, None
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.post(
    "/harness/sessions/{session_id}/unread",
    response={204: None, 403: dict, 404: dict},
    summary="Mark a harness session as unread",
)
def mark_harness_session_unread(request: HttpRequest, session_id: uuid.UUID):
    """Record that the user explicitly marked a harness session unread."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        service.mark_session_unread(session.id)
        return 204, None
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.post(
    "/harness/sessions/{session_id}/messages/{message_id}/notice-dismiss",
    response={204: None, 403: dict, 404: dict},
    summary="Dismiss the stopped/failed notice of a harness message",
)
def dismiss_harness_notice(
    request: HttpRequest, session_id: uuid.UUID, message_id: uuid.UUID
):
    """Persist that the user dismissed one stopped/failed notice."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        service.dismiss_notice(session.id, message_id)
        return 204, None
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.patch(
    "/harness/sessions/{session_id}/mode",
    response={200: HarnessSessionOut, 400: dict, 403: dict, 404: dict, 409: dict},
    summary="Switch a harness session's plan|build mode (idle only)",
)
async def set_harness_session_mode(
    request: HttpRequest, session_id: uuid.UUID, payload: HarnessModeIn
):
    """Switch mode on an idle session (409 when a run is active)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        if service.is_running(session.id):
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        updated = await sync_to_async(service.set_mode)(session.id, payload.mode)
        return 200, await sync_to_async(session_to_out_with_unread)(service, updated)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/abort",
    response={200: HarnessSessionOut, 403: dict, 404: dict, 409: dict},
    summary="Abort the active run of a harness session",
)
async def abort_harness_session(request: HttpRequest, session_id: uuid.UUID):
    """Cancel the active run task and mark message/parts aborted."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        aborted = await service.abort_run(session.id)
        return 200, await sync_to_async(session_to_out_with_unread)(service, aborted)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/harness/sessions/{session_id}/timeline",
    response={200: dict, 403: dict, 404: dict},
    summary="Load all session messages with lightweight part projections",
)
def get_harness_timeline(request: HttpRequest, session_id: uuid.UUID):
    """Return the session envelope without heavyweight detail fields."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        return 200, timeline_to_out(service, session)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/harness/sessions/{session_id}/parts/{part_id}",
    response={200: dict, 403: dict, 404: dict},
    summary="Load one full harness part by ID",
)
def get_harness_part(request: HttpRequest, session_id: uuid.UUID, part_id: uuid.UUID):
    """Return full detail for one part in an owned session."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        part = service.get_part(session.id, part_id)
        return 200, part_to_out(part)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/harness/sessions/{session_id}/todos",
    response={200: list[HarnessTodoOut], 403: dict, 404: dict},
    summary="List todos of a harness session",
)
def list_harness_todos(request: HttpRequest, session_id: uuid.UUID):
    """Return persisted todos of a session (owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        rows = service.list_todos(session.id)
        return 200, [
            HarnessTodoOut(
                id=row["id"],
                content=row["content"],
                status=row["status"],
                priority=row["priority"],
                order=row["order"],
            )
            for row in rows
        ]
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.post(
    "/harness/sessions/{session_id}/permissions/{request_id}",
    response={200: HarnessPermissionOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Resolve a harness permission request",
)
async def resolve_harness_permission(
    request: HttpRequest,
    session_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: HarnessPermissionResolveIn,
):
    """Resolve a pending permission request (once|always|reject)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PERMISSIONS):
        return _perm_denied(APIKeyPermission.HARNESS_PERMISSIONS)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        try:
            _require_claude_session_owner(session, request.user.id)
        except AuthenticationError as exc:
            return 403, {"detail": exc.message, "code": "permission_denied"}
        outcome = await service.resolve_permission(
            session=session,
            request_id=request_id,
            response=payload.response,
        )
        return 200, HarnessPermissionOut(
            request_id=request_id,
            decision=outcome["decision"],
            remember=outcome["remember"],
        )
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, LookupError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/questions/{question_id}",
    response={200: HarnessQuestionOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Answer a harness question request",
)
async def resolve_harness_question(
    request: HttpRequest,
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: HarnessQuestionResolveIn,
):
    """Submit answers for a pending question request (resumes the tool)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PERMISSIONS):
        return _perm_denied(APIKeyPermission.HARNESS_PERMISSIONS)
    from asgiref.sync import sync_to_async

    org_id = _get_org_id(request)
    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = await sync_to_async(service.get_session)(session_id)
        await sync_to_async(_owned_workspace)(request, org_id, session.workspace_id)
        try:
            _require_claude_session_owner(session, request.user.id)
        except AuthenticationError as exc:
            return 403, {"detail": exc.message, "code": "permission_denied"}
        outcome = await service.resolve_question(
            session=session,
            question_id=question_id,
            answers=payload.answers,
            reject=payload.reject,
        )
        return 200, HarnessQuestionOut(
            request_id=question_id,
            status=outcome["status"],
            resumed=bool(outcome.get("resumed", True)),
        )
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, LookupError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


# ---------------------------------------------------------------------------
# Provider config endpoints (org-scoped)
# ---------------------------------------------------------------------------


@harness_router.get(
    "/recent-models/",
    response={200: list[RecentModelOut], 401: dict, 403: dict, 404: dict},
    summary="List recently used composer models",
)
def list_recent_models(request: HttpRequest):
    """Return the user's recent models (newest first, capped server-side)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.repositories import RecentModelRepository

        rows = RecentModelRepository.list_by_user(org_id, request.user.id)
        return 200, [_recent_model_to_out(row) for row in rows]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/recent-models/",
    response={200: RecentModelOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Record a used composer model",
)
def save_recent_model(request: HttpRequest, payload: RecentModelSaveIn):
    """Upsert one recent-model row (LRU); called after a successful send."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.repositories import RecentModelRepository

        model_id = (payload.model or "").strip()
        if not model_id:
            return 400, {"detail": "model is required", "code": "validation_error"}
        row = RecentModelRepository.record_usage(
            org_id,
            request.user,
            model=model_id,
            effort=(payload.effort or "").strip(),
        )
        if row is None:
            return 400, {"detail": "model is required", "code": "validation_error"}
        return 200, _recent_model_to_out(row)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.get(
    "/agent-configs/",
    response={200: list[AgentConfigOut], 401: dict, 403: dict, 404: dict},
    summary="List per-agent model/effort configs",
)
def list_org_agent_configs(request: HttpRequest):
    """Return one row per configurable agent (defaults when unstored)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import AgentConfigService

        rows = AgentConfigService().list_configs(org_id)
        return 200, [_agent_config_to_out(row) for row in rows]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/agent-configs/",
    response={200: list[AgentConfigOut], 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Save (bulk upsert) per-agent model/effort configs",
)
def save_org_agent_configs(request: HttpRequest, payload: AgentConfigSaveIn):
    """Validate and upsert per-agent configs."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import AgentConfigService

        rows = AgentConfigService().save_configs(
            org_id,
            [item.model_dump() for item in payload.configs],
        )
        return 200, [_agent_config_to_out(row) for row in rows]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.get(
    "/subagent-config/",
    response={200: SubagentConfigOut, 401: dict, 403: dict, 404: dict},
    summary="Get the org-wide subagent depth config",
)
def get_org_subagent_config(request: HttpRequest):
    """Return the org subagent config (defaults when unstored)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import SubagentConfigService

        return 200, SubagentConfigOut(
            **SubagentConfigService().get_or_default(org_id)
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/subagent-config/",
    response={200: SubagentConfigOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Save (upsert) the org-wide subagent depth config",
)
def save_org_subagent_config(request: HttpRequest, payload: SubagentConfigIn):
    """Validate and upsert the org subagent config."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import SubagentConfigService

        return 200, SubagentConfigOut(
            **SubagentConfigService().save_config(org_id, payload.max_depth)
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.get(
    "/agent-s-config/",
    response={200: AgentSConfigOut, 401: dict, 403: dict, 404: dict},
    summary="Get the org-wide Agent-S harness config",
)
def get_org_agent_s_config(request: HttpRequest):
    """Return the org Agent-S config (defaults when unstored)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import AgentSConfigService

        return 200, agent_s_config_to_out(
            AgentSConfigService().get_or_default(org_id)
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/agent-s-config/",
    response={200: AgentSConfigOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Save (upsert) the org-wide Agent-S harness config",
)
def save_org_agent_s_config(request: HttpRequest, payload: AgentSConfigIn):
    """Validate and upsert the org Agent-S config."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import AgentSConfigService

        # Partial payloads merge: only explicitly sent fields are updated.
        row = AgentSConfigService().save_config(
            org_id, payload.model_dump(exclude_unset=True)
        )
        return 200, agent_s_config_to_out(row)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.get(
    "/provider-config/",
    response={200: ProviderConfigOut, 401: dict, 403: dict, 404: dict},
    summary="Get the org-wide model defaults",
)
def get_org_provider_config(request: HttpRequest):
    """Return the organization's model and reasoning-effort defaults."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import ProviderConfigService

        return 200, provider_config_to_out(
            ProviderConfigService().find_config(org_id)
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/provider-config/",
    response={200: ProviderConfigOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Save the org-wide model defaults",
)
def save_org_provider_config(request: HttpRequest, payload: ProviderConfigIn):
    """Save model defaults; provider credentials belong to named accounts."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import ProviderConfigService

        config = ProviderConfigService().save_config(
            organization_id=org_id, **payload.model_dump()
        )
        return 200, provider_config_to_out(config)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/provider-config/",
    response={204: None, 401: dict, 403: dict, 404: dict},
    summary="Delete the org-wide provider config",
)
def delete_org_provider_config(request: HttpRequest):
    """Delete the org provider config."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.services import ProviderConfigService

        ProviderConfigService().delete_config(org_id)
        return 204, None
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/provider-config/accounts/",
    response={200: list[ProviderAccountOut], 401: dict, 403: dict, 404: dict},
    summary="List visible connected provider accounts",
)
def list_provider_accounts(request: HttpRequest):
    """List connected private/shared accounts visible to the current user."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.provider_accounts import ProviderAccountService

        rows = ProviderAccountService().list_accounts(org_id, int(request.user.id))
        return 200, [ProviderAccountOut(**row) for row in rows if row["connected"]]
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.post(
    "/provider-config/accounts/{provider}/",
    response={201: ProviderAccountOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Create a named provider account",
)
def create_provider_account(
    request: HttpRequest, provider: str, payload: ProviderAccountIn
):
    """Create one private account or an admin-created org-shared account."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.provider_accounts import ProviderAccountService
        from apps.harness.providers.models_catalog import clear_models_cache

        if provider == "chatgpt":
            raise ValueError("ChatGPT accounts must be connected using OAuth")
        account = ProviderAccountService().save_account(
            organization_id=org_id,
            user=request.user,
            provider=provider,
            payload=payload.model_dump(exclude_unset=True),
        )
        clear_models_cache(str(org_id))
        row = next(
            item
            for item in ProviderAccountService().list_accounts(
                org_id, int(request.user.id), provider
            )
            if item["id"] == str(account.id)
        )
        return 201, ProviderAccountOut(**row)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.put(
    "/provider-config/accounts/{provider}/{account_id}/",
    response={200: ProviderAccountOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Update a named provider account",
)
def update_provider_account(
    request: HttpRequest,
    provider: str,
    account_id: uuid.UUID,
    payload: ProviderAccountIn,
):
    """Update an exact visible account after verifying its provider."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.provider_accounts import ProviderAccountService
        from apps.harness.providers.models_catalog import clear_models_cache

        service = ProviderAccountService()
        account = service.get_account(
            org_id, int(request.user.id), account_id, manage=True
        )
        if account.provider != provider:
            raise NotFoundError("ProviderAccount", str(account_id))
        updated = service.save_account(
            organization_id=org_id,
            user=request.user,
            provider=provider,
            payload=payload.model_dump(exclude_unset=True),
            account_id=account_id,
        )
        clear_models_cache(str(org_id))
        row = next(
            item
            for item in service.list_accounts(org_id, int(request.user.id), provider)
            if item["id"] == str(updated.id)
        )
        return 200, ProviderAccountOut(**row)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/provider-config/accounts/{provider}/{account_id}/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Delete a named provider account",
)
def delete_provider_account(request: HttpRequest, provider: str, account_id: uuid.UUID):
    """Delete only the exact account when visible and manageable."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.provider_accounts import ProviderAccountService
        from apps.harness.providers.models_catalog import clear_models_cache

        service = ProviderAccountService()
        account = service.get_account(
            org_id, int(request.user.id), account_id, manage=True
        )
        if account.provider != provider:
            raise NotFoundError("ProviderAccount", str(account_id))
        service.delete_account(org_id, int(request.user.id), account_id)
        clear_models_cache(str(org_id))
        return 204, None
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.put(
    "/provider-config/selection/{provider}/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Select the active provider account for this user and organization",
)
def select_provider_account(
    request: HttpRequest, provider: str, payload: ProviderSelectionIn
):
    """Select one visible account or explicitly select no account."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        from apps.harness.provider_accounts import ProviderAccountService

        ProviderAccountService().select_account(
            org_id, int(request.user.id), provider, payload.account_id
        )
        return 204, None
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/provider-config/providers/chatgpt/oauth/start/",
    response={
        200: ChatGPTOAuthStartOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        502: dict,
    },
    summary="Start ChatGPT device OAuth flow",
)
async def start_chatgpt_oauth(request: HttpRequest):
    """Start an isolated durable ChatGPT flow for the current user."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    from apps.harness.provider_oauth import ProviderOAuthService
    from apps.harness.providers.base import ProviderError

    try:
        body = request.body
        is_json = (request.content_type or "").lower().startswith("application/json")
        raw = json.loads(body.decode("utf-8")) if is_json and body.strip() else {}
        payload = ChatGPTOAuthStartIn(**raw)
        org_id = _get_org_id(request)
        result = await ProviderOAuthService().start(
            organization_id=org_id,
            user=request.user,
            label=payload.label,
            shared=payload.shared,
            account_id=payload.account_id,
        )
        return 200, ChatGPTOAuthStartOut(**result)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except ProviderError:
        return 502, {
            "detail": "ChatGPT OAuth provider request failed",
            "code": "provider_error",
        }


@harness_router.get(
    "/provider-config/providers/chatgpt/oauth/status/",
    response={
        200: ChatGPTOAuthStatusOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        410: dict,
        502: dict,
    },
    summary="Poll ChatGPT device OAuth status (single attempt)",
)
async def poll_chatgpt_oauth_status(
    request: HttpRequest, flow_id: uuid.UUID | None = None
):
    """Poll one user's flow once; flow IDs are private to their creator."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    from apps.harness.provider_oauth import ProviderOAuthService
    from apps.harness.providers.base import ProviderError

    try:
        result = await ProviderOAuthService().status(
            organization_id=_get_org_id(request), user=request.user, flow_id=flow_id
        )
        if result["status"] == "no_flow":
            return 404, {
                **result,
                "detail": "No pending ChatGPT OAuth flow",
                "code": "no_flow",
            }
        if result["status"] == "expired":
            return 410, result
        return 200, ChatGPTOAuthStatusOut(**result)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except ProviderError:
        return 502, {
            "detail": "ChatGPT OAuth provider request failed",
            "code": "provider_error",
        }


@harness_router.post(
    "/provider-config/providers/chatgpt/oauth/cancel/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Cancel one pending ChatGPT OAuth flow",
)
async def cancel_chatgpt_oauth(request: HttpRequest):
    """Cancel one owned flow; no flow ID is accepted only when unambiguous."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    from apps.harness.provider_oauth import ProviderOAuthService

    try:
        body = request.body
        is_json = (request.content_type or "").lower().startswith("application/json")
        raw = json.loads(body.decode("utf-8")) if is_json and body.strip() else {}
        payload = ChatGPTOAuthCancelIn(**raw)
        raw_id = payload.flow_id
        from asgiref.sync import sync_to_async

        cancelled = await sync_to_async(ProviderOAuthService().cancel)(
            organization_id=_get_org_id(request), user=request.user, flow_id=raw_id
        )
        if not cancelled:
            return 404, {"detail": "No pending OAuth flow", "code": "no_flow"}
        return 204, None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
