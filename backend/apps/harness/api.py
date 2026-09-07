"""
REST API for the harness sessions (M6, additive).

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

All old ``session:*`` socket events and runners REST endpoints stay
untouched (M8 removes them).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import decrypt_value

harness_router = Router(tags=["harness"])


def _perm_denied(permission: APIKeyPermission):  # type: ignore[no-untyped-def]
    """Return a 403 error tuple for a denied API key permission."""
    return 403, {
        "detail": f"API key lacks permission: {permission.value}",
        "code": "permission_denied",
    }


def _get_service():  # type: ignore[no-untyped-def]
    """Return the runners service (owner scoping lives there)."""
    from apps.runners.sio_server import get_runner_service

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


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HarnessSessionCreateIn(Schema):
    """Request schema for starting a harness session."""

    prompt: str
    agent_name: str = "build"
    mode: str = "build"
    model: str = ""
    reasoning_effort: str = ""
    skill_ids: list[str] = []


class HarnessSessionPatchIn(Schema):
    """Request schema for updating a harness session."""

    title: str = ""


class HarnessConversationOut(Schema):
    """Response schema for org-wide harness conversations."""

    session_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    title: str
    status: str
    mode: str
    agent_name: str
    model: str = ""
    reasoning_effort: str = ""
    unread: bool = False
    updated_at: datetime


class HarnessMessageIn(Schema):
    """Request schema for a follow-up prompt on a session."""

    prompt: str
    mode: str = ""
    model: str = ""
    reasoning_effort: str = ""
    skill_ids: list[str] = []


class HarnessModeIn(Schema):
    """Request schema for switching a session's plan|build mode."""

    mode: str


class HarnessPermissionResolveIn(Schema):
    """Request schema for resolving a permission request."""

    response: str


class HarnessQuestionResolveIn(Schema):
    """Request schema for answering a harness question request."""

    answers: list[Any] = []
    reject: bool = False


class HarnessQuestionOut(Schema):
    """Response schema for a resolved question request."""

    request_id: uuid.UUID
    status: str


class HarnessSessionOut(Schema):
    """Response schema for a harness session."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title: str
    mode: str
    agent_name: str
    model: str
    reasoning_effort: str = ""
    status: str
    cost: float
    tokens: dict = {}
    skill_ids: list[str] = []
    unread: bool = False
    created_at: datetime
    updated_at: datetime


class HarnessMessageOut(Schema):
    """Response schema for a harness message."""

    id: uuid.UUID
    role: str
    content: str
    model: str
    reasoning_effort: str = ""
    cost: float
    tokens: dict = {}
    finish: str
    error: str
    created_at: datetime
    completed_at: datetime | None = None


class HarnessPartOut(Schema):
    """Response schema for a harness part."""

    id: uuid.UUID
    message_id: uuid.UUID
    type: str
    state: str
    call_id: str
    title: str
    output: str
    input: dict = {}
    meta: dict = {}


class HarnessTodoOut(Schema):
    """Response schema for a harness todo."""

    id: uuid.UUID
    content: str
    status: str
    priority: str
    order: int


class HarnessPermissionOut(Schema):
    """Response schema for a resolved permission request."""

    request_id: uuid.UUID
    decision: str
    remember: str


class ProviderConfigIn(Schema):
    """Request schema for saving the org-wide provider config."""

    api_key: str = ""
    base_url: str = ""
    default_model: str = ""
    small_model: str = ""
    computer_use_model: str = ""


class ProviderConfigOut(Schema):
    """Response schema for the org-wide provider config (no secret)."""

    base_url: str
    default_model: str
    small_model: str
    computer_use_model: str
    has_api_key: bool
    api_key_hint: str


class ProviderModelOut(Schema):
    """One model from the org provider catalog."""

    id: str
    name: str
    reasoning_efforts: list[str] = []
    default_effort: str = ""
    supports_tools: bool = False
    context_length: int = 0
    max_output_tokens: int = 0


def _api_key_hint(config) -> str:  # type: ignore[no-untyped-def]
    """Return a masked hint (last four chars) when a key is stored."""
    if not config.api_key_encrypted:
        return ""
    try:
        plain = decrypt_value(config.api_key_encrypted)
    except Exception:
        return "••••"
    suffix = plain[-4:] if len(plain) >= 4 else plain
    return f"••••{suffix}"


def _provider_config_to_out(config) -> ProviderConfigOut:  # type: ignore[no-untyped-def]
    """Map a ProviderConfig ORM row to ProviderConfigOut (never plaintext)."""
    return ProviderConfigOut(
        base_url=config.base_url or "",
        default_model=config.default_model or "",
        small_model=config.small_model or "",
        computer_use_model=config.computer_use_model or "",
        has_api_key=bool(config.api_key_encrypted),
        api_key_hint=_api_key_hint(config),
    )


def _fetch_org_provider_config(org_id: uuid.UUID) -> ProviderConfigOut:
    """Load the org provider config and map to the public response."""
    from apps.harness.services import ProviderConfigService

    config = ProviderConfigService().get_config(org_id)
    return _provider_config_to_out(config)


def _list_org_provider_models(org_id: uuid.UUID) -> list[ProviderModelOut]:
    """Fetch the OpenRouter catalog for an organization."""
    from apps.harness.services import ProviderConfigService

    models = ProviderConfigService().list_models(org_id)
    return [
        ProviderModelOut(
            id=model.id,
            name=model.name,
            reasoning_efforts=list(model.reasoning_efforts),
            default_effort=model.default_effort,
            supports_tools=model.supports_tools,
            context_length=model.context_length,
            max_output_tokens=model.max_output_tokens,
        )
        for model in models
    ]


def _save_org_provider_config(
    org_id: uuid.UUID, payload: ProviderConfigIn
) -> ProviderConfigOut:
    """Upsert the org provider config and map to the public response."""
    from apps.harness.services import ProviderConfigService

    config = ProviderConfigService().save_config(
        organization_id=org_id,
        api_key=payload.api_key or "",
        base_url=payload.base_url or "",
        default_model=payload.default_model or "",
        small_model=payload.small_model or "",
        computer_use_model=payload.computer_use_model or "",
    )
    return _provider_config_to_out(config)


def _delete_org_provider_config(org_id: uuid.UUID) -> None:
    """Delete the org provider config."""
    from apps.harness.services import ProviderConfigService

    ProviderConfigService().delete_config(org_id)


def _session_to_out(session, *, unread: bool = False) -> HarnessSessionOut:  # type: ignore[no-untyped-def]
    """Map a HarnessSession ORM row to HarnessSessionOut."""
    return HarnessSessionOut(
        id=session.id,
        workspace_id=session.workspace_id,
        parent_id=session.parent_id,
        title=session.title or "",
        mode=session.mode,
        agent_name=session.agent_name,
        model=session.model or "",
        reasoning_effort=session.reasoning_effort or "",
        status=session.status,
        cost=float(session.cost or 0.0),
        tokens=dict(session.tokens or {}),
        skill_ids=[str(skill_id) for skill_id in (session.skill_ids or [])],
        unread=unread,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _session_to_out_with_unread(service, session) -> HarnessSessionOut:  # type: ignore[no-untyped-def]
    """Map a session and compute its unread flag."""
    return _session_to_out(session, unread=service.is_session_unread(session))


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
        return 200, [
            _session_to_out(session, unread=unread_map.get(session.id, False))
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
        return 201, await sync_to_async(_session_to_out_with_unread)(service, fresh)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/message",
    response={202: HarnessSessionOut, 400: dict, 403: dict, 404: dict, 409: dict},
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
        return 202, await sync_to_async(_session_to_out_with_unread)(service, fresh)
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
        updated = await sync_to_async(service.update_title)(
            session.id, payload.title
        )
        return 200, await sync_to_async(_session_to_out_with_unread)(
            service, updated
        )
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/harness/sessions/{session_id}",
    response={204: None, 403: dict, 404: dict},
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
    rows = service.list_conversations(
        organization_id=org_id,
        workspace_ids=[workspace.id for workspace in workspaces],
    )
    return 200, [
        HarnessConversationOut(
            session_id=uuid.UUID(row["session_id"]),
            workspace_id=uuid.UUID(row["workspace_id"]),
            workspace_name=row["workspace_name"],
            title=row["title"],
            status=row["status"],
            mode=row["mode"],
            agent_name=row["agent_name"],
            model=row.get("model") or "",
            reasoning_effort=row.get("reasoning_effort") or "",
            unread=row["unread"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
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
        return 200, await sync_to_async(_session_to_out_with_unread)(
            service, updated
        )
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/abort",
    response={200: HarnessSessionOut, 403: dict, 404: dict},
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
        return 200, await sync_to_async(_session_to_out_with_unread)(
            service, aborted
        )
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/harness/sessions/{session_id}/parts",
    response={200: dict, 403: dict, 404: dict},
    summary="List messages and parts of a harness session",
)
def list_harness_parts(request: HttpRequest, session_id: uuid.UUID):
    """Return messages with their streamed parts and pending user gates."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        messages = service.list_messages(session.id)
        parts = service.list_parts(session.id)
        parts_by_message: dict[str, list] = {}
        for part in parts:
            parts_by_message.setdefault(str(part.message_id), []).append(
                HarnessPartOut(
                    id=part.id,
                    message_id=part.message_id,
                    type=part.type,
                    state=part.state,
                    call_id=part.call_id or "",
                    title=part.title or "",
                    output=part.output or "",
                    input=dict(part.input or {}),
                    meta=dict(part.meta or {}),
                )
            )
        return 200, {
            "session": _session_to_out_with_unread(service, session).model_dump(
                mode="json"
            ),
            "messages": [
                {
                    **HarnessMessageOut(
                        id=message.id,
                        role=message.role,
                        content=message.content or "",
                        model=message.model or "",
                        reasoning_effort=message.reasoning_effort or "",
                        cost=float(message.cost or 0.0),
                        tokens=dict(message.tokens or {}),
                        finish=message.finish or "",
                        error=message.error or "",
                        created_at=message.created_at,
                        completed_at=message.completed_at,
                    ).model_dump(mode="json"),
                    "parts": [
                        part.model_dump(mode="json")
                        for part in parts_by_message.get(str(message.id), [])
                    ],
                }
                for message in messages
            ],
            "permissions": service.list_pending_permissions(
                session.id, include_descendants=True
            ),
            "questions": service.list_pending_questions(
                session.id, include_descendants=True
            ),
        }
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
    response={200: HarnessPermissionOut, 400: dict, 403: dict, 404: dict},
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
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, LookupError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/harness/sessions/{session_id}/questions/{question_id}",
    response={200: HarnessQuestionOut, 400: dict, 403: dict, 404: dict},
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
        outcome = await service.resolve_question(
            session=session,
            question_id=question_id,
            answers=payload.answers,
            reject=payload.reject,
        )
        return 200, HarnessQuestionOut(
            request_id=question_id,
            status=outcome["status"],
        )
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, LookupError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


# ---------------------------------------------------------------------------
# Provider config endpoints (org-scoped; workspace paths are aliases)
# ---------------------------------------------------------------------------


@harness_router.get(
    "/provider-config/",
    response={200: ProviderConfigOut, 401: dict, 403: dict, 404: dict},
    summary="Get the org-wide provider config (api key never returned)",
)
def get_org_provider_config(request: HttpRequest):
    """Return the org provider config (base_url/models only)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        return 200, _fetch_org_provider_config(org_id)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/provider-config/models/",
    response={200: list[ProviderModelOut], 401: dict, 403: dict, 404: dict, 502: dict},
    summary="List models from the org OpenRouter provider",
)
def list_org_provider_models(request: HttpRequest):
    """Proxy OpenRouter GET /models for the org provider config."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        return 200, _list_org_provider_models(org_id)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except Exception as exc:
        from apps.harness.providers.base import (
            ProviderAuthError,
            ProviderResponseError,
            ProviderTimeoutError,
        )

        if isinstance(
            exc, (ProviderAuthError, ProviderResponseError, ProviderTimeoutError)
        ):
            return 502, {"detail": str(exc), "code": "provider_error"}
        raise


@harness_router.put(
    "/provider-config/",
    response={200: ProviderConfigOut, 400: dict, 401: dict, 403: dict},
    summary="Save (upsert) the org-wide provider config",
)
def save_org_provider_config(request: HttpRequest, payload: ProviderConfigIn):
    """Upsert the org provider config via Fernet encryption."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        return 200, _save_org_provider_config(org_id, payload)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
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
        _delete_org_provider_config(org_id)
        return 204, None
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/workspaces/{workspace_id}/provider-config/",
    response={200: ProviderConfigOut, 403: dict, 404: dict},
    summary="Get the org-wide provider config (api key never returned)",
)
def get_provider_config(request: HttpRequest, workspace_id: uuid.UUID):
    """Return the org provider config (base_url/models only, owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    try:
        OrganizationService().require_membership(request.user, org_id)
        _owned_workspace(request, org_id, workspace_id)
        return 200, _fetch_org_provider_config(org_id)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/workspaces/{workspace_id}/provider-config/",
    response={200: ProviderConfigOut, 400: dict, 403: dict, 404: dict},
    summary="Save (upsert) the org-wide provider config",
)
def save_provider_config(
    request: HttpRequest, workspace_id: uuid.UUID, payload: ProviderConfigIn
):
    """Upsert the org provider config via Fernet encryption (owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    org_id = _get_org_id(request)
    try:
        OrganizationService().require_membership(request.user, org_id)
        _owned_workspace(request, org_id, workspace_id)
        return 200, _save_org_provider_config(org_id, payload)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/workspaces/{workspace_id}/provider-config/",
    response={204: None, 403: dict, 404: dict},
    summary="Delete the org-wide provider config",
)
def delete_provider_config(request: HttpRequest, workspace_id: uuid.UUID):
    """Delete the org provider config (owner-scoped)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    org_id = _get_org_id(request)
    try:
        OrganizationService().require_membership(request.user, org_id)
        _owned_workspace(request, org_id, workspace_id)
        _delete_org_provider_config(org_id)
        return 204, None
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
