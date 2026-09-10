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
- ``harness:providers`` — manage per-provider connections (OpenRouter,
  ChatGPT OAuth, Amazon Bedrock)

All old ``session:*`` socket events and runners REST endpoints stay
untouched (M8 removes them).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.harness.enums import ProviderType
from apps.harness.providers.openrouter import DEFAULT_BASE_URL
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

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
    manual_unread: bool = False
    needs_attention: bool = False
    attention_kind: str = ""
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
    manual_unread: bool = False
    needs_attention: bool = False
    attention_kind: str = ""
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
    """Request schema for saving the org-wide provider config.

    deprecated: use agent-configs. Fields below remain for backward compat.
    """

    api_key: str = ""
    base_url: str = ""
    default_model: str = ""
    small_model: str = ""
    computer_use_model: str = ""
    default_effort: str = ""
    small_effort: str = ""
    computer_use_effort: str = ""


class ProviderConfigOut(Schema):
    """Response schema for the org-wide provider config (no secret).

    deprecated: use agent-configs. Fields remain for backward compat.
    """

    base_url: str
    default_model: str
    small_model: str
    computer_use_model: str
    default_effort: str
    small_effort: str
    computer_use_effort: str
    has_api_key: bool
    api_key_hint: str


class AgentConfigIn(Schema):
    """Request schema for one agent config row."""

    agent: str
    model: str = ""
    effort: str = ""
    inherit_model: bool = False
    effort_strategy: str = "fixed"


class AgentConfigSaveIn(Schema):
    """Bulk save payload for agent configs."""

    configs: list[AgentConfigIn] = []


class AgentConfigOut(Schema):
    """Response schema for one agent config row."""

    agent: str
    mode: str
    description: str
    model: str = ""
    effort: str = ""
    inherit_model: bool = False
    effort_strategy: str = "fixed"


class ProviderModelOut(Schema):
    """One model from the org provider catalog."""

    id: str
    name: str
    provider: str = ""
    reasoning_efforts: list[str] = []
    default_effort: str = ""
    supports_tools: bool = False
    context_length: int = 0
    max_output_tokens: int = 0


class ProviderConnectionOut(Schema):
    """Public view of one provider connection (no secrets)."""

    provider: str
    connected: bool
    base_url: str = ""
    api_key_hint: str = ""
    account_id: str = ""
    region: str = ""
    auth_method: str = ""


class OpenRouterConnectionIn(Schema):
    """Upsert payload for an OpenRouter connection."""

    api_key: str = ""
    base_url: str = ""


class BedrockConnectionIn(Schema):
    """Upsert payload for an Amazon Bedrock connection."""

    auth_method: str = ""
    region: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    bearer_token: str = ""


class ProviderConnectionUpsertIn(Schema):
    """Combined upsert payload; validated per provider in the handler."""

    api_key: str = ""
    base_url: str = ""
    auth_method: str = ""
    region: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    bearer_token: str = ""


class ChatGPTOAuthStartOut(Schema):
    """Response for starting the ChatGPT device OAuth flow."""

    user_code: str
    verification_url: str
    interval: int
    expires_in: int


class ChatGPTOAuthStatusOut(Schema):
    """Response for polling ChatGPT device OAuth status."""

    status: str
    account_id: str = ""


CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS = 600

# Single-process in-memory store for pending ChatGPT OAuth flows. State is
# lost on process restart and is not shared across multiple workers.
@dataclass
class _PendingChatGPTOAuth:
    """Server-side state for an in-progress ChatGPT device flow."""

    device_auth_id: str
    user_code: str
    interval: int
    created_at: float


_chatgpt_oauth_pending: dict[uuid.UUID, _PendingChatGPTOAuth] = {}
_chatgpt_oauth_lock = asyncio.Lock()

_VALID_PROVIDERS = {choice.value for choice in ProviderType}


def _secret_hint(secret: str) -> str:
    """Return a masked hint (last four chars) for a stored secret."""
    if not secret:
        return ""
    suffix = secret[-4:] if len(secret) >= 4 else secret
    return f"••••{suffix}"


def _openrouter_connection_view(
    org_id: uuid.UUID,
) -> tuple[bool, str, str]:
    """Return ``(has_api_key, api_key_hint, base_url)`` for OpenRouter."""
    from apps.harness.services import ProviderConfigService

    service = ProviderConfigService()
    connection = service.get_connection(org_id, ProviderType.OPENROUTER)
    if connection is None:
        return False, "", DEFAULT_BASE_URL
    credentials = service.get_connection_credentials(connection)
    api_key = str(credentials.get("api_key", "") or "")
    has_key = bool(api_key.strip())
    base_url = str((connection.config or {}).get("base_url") or DEFAULT_BASE_URL)
    return has_key, _secret_hint(api_key) if has_key else "", base_url


def _provider_config_to_out(config, org_id: uuid.UUID) -> ProviderConfigOut:  # type: ignore[no-untyped-def]
    """Map a ProviderConfig ORM row to ProviderConfigOut (never plaintext)."""
    has_key, hint, base_url = _openrouter_connection_view(org_id)
    return ProviderConfigOut(
        base_url=base_url,
        default_model=config.default_model or "",
        small_model=config.small_model or "",
        computer_use_model=config.computer_use_model or "",
        default_effort=config.default_effort or "",
        small_effort=config.small_effort or "",
        computer_use_effort=config.computer_use_effort or "",
        has_api_key=has_key,
        api_key_hint=hint,
    )


def _parse_provider_param(provider: str) -> str:
    """Validate a provider path segment."""
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    return provider


def _connection_to_out(
    service,  # type: ignore[no-untyped-def]
    connection,
) -> ProviderConnectionOut:
    """Map a stored connection to the public list/detail shape."""
    provider = connection.provider
    credentials = service.get_connection_credentials(connection)
    config = dict(connection.config or {})
    if provider == ProviderType.OPENROUTER:
        api_key = str(credentials.get("api_key", "") or "")
        return ProviderConnectionOut(
            provider=provider,
            connected=True,
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            api_key_hint=_secret_hint(api_key),
        )
    if provider == ProviderType.CHATGPT:
        return ProviderConnectionOut(
            provider=provider,
            connected=True,
            account_id=str(credentials.get("account_id", "") or ""),
        )
    if provider == ProviderType.AMAZON_BEDROCK:
        auth_method = str(config.get("auth_method") or "")
        if not auth_method:
            auth_method = (
                "bearer"
                if str(credentials.get("bearer_token", "") or "").strip()
                else "access_keys"
            )
        return ProviderConnectionOut(
            provider=provider,
            connected=True,
            region=str(config.get("region") or "us-east-1"),
            auth_method=auth_method,
        )
    return ProviderConnectionOut(provider=provider, connected=True)


def _list_org_provider_connections(org_id: uuid.UUID) -> list[ProviderConnectionOut]:
    """Return connection status for every supported provider."""
    from apps.harness.services import ProviderConfigService

    service = ProviderConfigService()
    by_provider = {
        connection.provider: connection
        for connection in service.list_connections(org_id)
    }
    rows: list[ProviderConnectionOut] = []
    for provider_type in ProviderType:
        provider = provider_type.value
        connection = by_provider.get(provider)
        if connection is None:
            rows.append(ProviderConnectionOut(provider=provider, connected=False))
            continue
        rows.append(_connection_to_out(service, connection))
    return rows


def _upsert_openrouter_connection(
    org_id: uuid.UUID,
    payload: OpenRouterConnectionIn,
) -> ProviderConnectionOut:
    """Create or update the OpenRouter connection for an organization."""
    from apps.harness.services import ProviderConfigService

    service = ProviderConfigService()
    existing = service.get_connection(org_id, ProviderType.OPENROUTER)
    credentials = (
        service.get_connection_credentials(existing) if existing is not None else {}
    )
    api_key = str(credentials.get("api_key", "") or "")
    if payload.api_key and payload.api_key.strip():
        api_key = payload.api_key.strip()
    elif existing is None and not api_key:
        raise ValueError("api_key is required when creating an OpenRouter connection")

    base_url = (payload.base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    if existing is not None and not (payload.base_url or "").strip():
        base_url = str((existing.config or {}).get("base_url") or DEFAULT_BASE_URL)

    connection = service.save_connection(
        organization_id=org_id,
        provider=ProviderType.OPENROUTER,
        credentials={"api_key": api_key},
        config={"base_url": base_url},
    )
    return _connection_to_out(service, connection)


def _upsert_bedrock_connection(
    org_id: uuid.UUID,
    payload: BedrockConnectionIn,
) -> ProviderConnectionOut:
    """Create or update the Amazon Bedrock connection for an organization."""
    from apps.harness.services import ProviderConfigService

    auth_method = (payload.auth_method or "").strip()
    if auth_method not in ("access_keys", "bearer"):
        raise ValueError("auth_method must be 'access_keys' or 'bearer'")

    service = ProviderConfigService()
    existing = service.get_connection(org_id, ProviderType.AMAZON_BEDROCK)
    credentials = (
        service.get_connection_credentials(existing) if existing is not None else {}
    )
    region = (payload.region or "us-east-1").strip() or "us-east-1"

    if auth_method == "access_keys":
        access_key_id = (
            payload.access_key_id.strip()
            if payload.access_key_id and payload.access_key_id.strip()
            else str(credentials.get("access_key_id", "") or "")
        )
        secret_access_key = (
            payload.secret_access_key.strip()
            if payload.secret_access_key and payload.secret_access_key.strip()
            else str(credentials.get("secret_access_key", "") or "")
        )
        session_token = (
            payload.session_token.strip()
            if payload.session_token and payload.session_token.strip()
            else str(credentials.get("session_token", "") or "")
        )
        if not access_key_id or not secret_access_key:
            raise ValueError(
                "access_key_id and secret_access_key are required for access_keys auth"
            )
        new_credentials = {
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
        }
        if session_token:
            new_credentials["session_token"] = session_token
    else:
        bearer_token = (
            payload.bearer_token.strip()
            if payload.bearer_token and payload.bearer_token.strip()
            else str(credentials.get("bearer_token", "") or "")
        )
        if not bearer_token:
            raise ValueError("bearer_token is required for bearer auth")
        new_credentials = {"bearer_token": bearer_token}

    connection = service.save_connection(
        organization_id=org_id,
        provider=ProviderType.AMAZON_BEDROCK,
        credentials=new_credentials,
        config={"region": region, "auth_method": auth_method},
    )
    return _connection_to_out(service, connection)


def _delete_org_provider_connection(org_id: uuid.UUID, provider: str) -> None:
    """Delete one provider connection for an organization."""
    from apps.harness.services import ProviderConfigService

    ProviderConfigService().delete_connection(org_id, provider)


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


def _fetch_org_provider_config(org_id: uuid.UUID) -> ProviderConfigOut:
    """Load the org provider config and map to the public response."""
    from apps.harness.services import ProviderConfigService

    config = ProviderConfigService().get_config(org_id)
    return _provider_config_to_out(config, org_id)


def _list_org_provider_models(org_id: uuid.UUID) -> list[ProviderModelOut]:
    """Fetch the merged provider catalog for an organization."""
    from apps.harness.services import ProviderConfigService

    models = ProviderConfigService().list_models(org_id)
    return [
        ProviderModelOut(
            id=model.id,
            name=model.name,
            provider=model.provider,
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
        default_effort=payload.default_effort or "",
        small_effort=payload.small_effort or "",
        computer_use_effort=payload.computer_use_effort or "",
    )
    return _provider_config_to_out(config, org_id)


def _delete_org_provider_config(org_id: uuid.UUID) -> None:
    """Delete the org default-model config only (connections are kept).

    Provider credentials in ``ProviderConnection`` rows are intentionally
    left intact so deleting model defaults does not disconnect providers.
    """
    from apps.harness.services import ProviderConfigService

    ProviderConfigService().delete_config(org_id)


def _session_to_out(
    session,
    *,
    unread: bool = False,
    manual_unread: bool = False,
    needs_attention: bool = False,
    attention_kind: str = "",
) -> HarnessSessionOut:  # type: ignore[no-untyped-def]
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
        manual_unread=manual_unread,
        needs_attention=needs_attention,
        attention_kind=attention_kind,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _session_to_out_with_unread(service, session) -> HarnessSessionOut:  # type: ignore[no-untyped-def]
    """Map a session and compute its unread and attention flags."""
    attention = service.attention_for_sessions([session]).get(session.id, {})
    return _session_to_out(
        session,
        unread=service.is_session_unread(session),
        manual_unread=session.manual_unread_at is not None,
        needs_attention=attention.get("needs_attention", False),
        attention_kind=attention.get("attention_kind", ""),
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
            _session_to_out(
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
            manual_unread=bool(row.get("manual_unread")),
            needs_attention=bool(row.get("needs_attention")),
            attention_kind=row.get("attention_kind") or "",
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
    summary="List models from all connected org providers",
)
def list_org_provider_models(request: HttpRequest):
    """Return the merged model catalog for connected org providers."""
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
    "/provider-config/providers/",
    response={200: list[ProviderConnectionOut], 401: dict, 403: dict, 404: dict},
    summary="List all provider connection statuses for the org",
)
def list_org_provider_connections(request: HttpRequest):
    """Return connection status for OpenRouter, ChatGPT, and Amazon Bedrock."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        return 200, _list_org_provider_connections(org_id)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/provider-config/providers/{provider}/",
    response={200: ProviderConnectionOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Upsert a provider connection for the org",
)
def upsert_org_provider_connection(
    request: HttpRequest,
    provider: str,
    payload: ProviderConnectionUpsertIn,
):
    """Create or update credentials for one provider (ChatGPT uses OAuth)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        provider_id = _parse_provider_param(provider)
        if provider_id == ProviderType.CHATGPT:
            return 400, {
                "detail": "ChatGPT uses OAuth connect; use the OAuth endpoints",
                "code": "validation_error",
            }
        if provider_id == ProviderType.OPENROUTER:
            body = OpenRouterConnectionIn(
                api_key=payload.api_key,
                base_url=payload.base_url,
            )
            return 200, _upsert_openrouter_connection(org_id, body)
        body = BedrockConnectionIn(
            auth_method=payload.auth_method,
            region=payload.region,
            access_key_id=payload.access_key_id,
            secret_access_key=payload.secret_access_key,
            session_token=payload.session_token,
            bearer_token=payload.bearer_token,
        )
        return 200, _upsert_bedrock_connection(org_id, body)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, KeyError, TypeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.delete(
    "/provider-config/providers/{provider}/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Delete a provider connection for the org",
)
def delete_org_provider_connection(request: HttpRequest, provider: str):
    """Remove one provider connection and invalidate the models cache."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        provider_id = _parse_provider_param(provider)
        _delete_org_provider_connection(org_id, provider_id)
        return 204, None
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@harness_router.post(
    "/provider-config/providers/chatgpt/oauth/start/",
    response={200: ChatGPTOAuthStartOut, 401: dict, 403: dict, 404: dict, 502: dict},
    summary="Start ChatGPT device OAuth flow",
)
async def start_chatgpt_oauth(request: HttpRequest):
    """Begin the ChatGPT device authorization flow for the org."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    from asgiref.sync import sync_to_async

    from apps.harness.providers.base import ProviderError

    try:
        org_id = _get_org_id(request)
        org_service = OrganizationService()
        await sync_to_async(org_service.require_membership)(request.user, org_id)
        from apps.harness.providers.chatgpt_oauth import start_device_flow

        flow = await start_device_flow()
        async with _chatgpt_oauth_lock:
            _chatgpt_oauth_pending[org_id] = _PendingChatGPTOAuth(
                device_auth_id=flow.device_auth_id,
                user_code=flow.user_code,
                interval=flow.interval,
                created_at=time.monotonic(),
            )
        return 200, ChatGPTOAuthStartOut(
            user_code=flow.user_code,
            verification_url=flow.verification_url,
            interval=flow.interval,
            expires_in=CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS,
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ProviderError as exc:
        return 502, {"detail": str(exc), "code": "provider_error"}


@harness_router.get(
    "/provider-config/providers/chatgpt/oauth/status/",
    response={
        200: ChatGPTOAuthStatusOut,
        401: dict,
        403: dict,
        404: dict,
        410: dict,
        502: dict,
    },
    summary="Poll ChatGPT device OAuth status (single attempt)",
)
async def poll_chatgpt_oauth_status(request: HttpRequest):
    """Poll once for ChatGPT OAuth completion and persist tokens on success."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    from asgiref.sync import sync_to_async

    from apps.harness.providers.base import ProviderError
    from apps.harness.providers.chatgpt_oauth import poll_device_flow_once

    try:
        org_id = _get_org_id(request)
        org_service = OrganizationService()
        await sync_to_async(org_service.require_membership)(request.user, org_id)
        async with _chatgpt_oauth_lock:
            pending = _chatgpt_oauth_pending.get(org_id)
        if pending is None:
            return 404, {
                "status": "no_flow",
                "detail": "No pending ChatGPT OAuth flow for this organization",
                "code": "no_flow",
            }
        if (
            time.monotonic() - pending.created_at
            > CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS
        ):
            async with _chatgpt_oauth_lock:
                _chatgpt_oauth_pending.pop(org_id, None)
            return 410, ChatGPTOAuthStatusOut(status="expired")

        result = await poll_device_flow_once(
            pending.device_auth_id,
            pending.user_code,
        )
        if result.status == "pending":
            return 200, ChatGPTOAuthStatusOut(status="pending")
        if result.status == "expired":
            async with _chatgpt_oauth_lock:
                _chatgpt_oauth_pending.pop(org_id, None)
            return 410, ChatGPTOAuthStatusOut(status="expired")
        if result.status == "denied":
            async with _chatgpt_oauth_lock:
                _chatgpt_oauth_pending.pop(org_id, None)
            return 410, ChatGPTOAuthStatusOut(status="denied")
        if result.tokens is None:
            return 502, {
                "detail": "OAuth completed without tokens",
                "code": "provider_error",
            }

        from apps.harness.services import ProviderConfigService

        credentials = {
            "access": result.tokens.access,
            "refresh": result.tokens.refresh,
            "expires": result.tokens.expires,
            "account_id": result.tokens.account_id or "",
            "residency": result.tokens.residency or "",
        }
        service = ProviderConfigService()
        await sync_to_async(service.save_connection)(
            organization_id=org_id,
            provider=ProviderType.CHATGPT,
            credentials=credentials,
            config={},
        )
        async with _chatgpt_oauth_lock:
            _chatgpt_oauth_pending.pop(org_id, None)
        return 200, ChatGPTOAuthStatusOut(
            status="connected",
            account_id=result.tokens.account_id or "",
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ProviderError as exc:
        return 502, {"detail": str(exc), "code": "provider_error"}


@harness_router.post(
    "/provider-config/providers/chatgpt/oauth/cancel/",
    response={204: None, 401: dict, 403: dict, 404: dict},
    summary="Cancel a pending ChatGPT OAuth flow",
)
async def cancel_chatgpt_oauth(request: HttpRequest):
    """Drop any in-memory pending ChatGPT OAuth state for the org."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    from asgiref.sync import sync_to_async

    try:
        org_id = _get_org_id(request)
        org_service = OrganizationService()
        await sync_to_async(org_service.require_membership)(request.user, org_id)
        async with _chatgpt_oauth_lock:
            _chatgpt_oauth_pending.pop(org_id, None)
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
