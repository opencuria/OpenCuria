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
  ChatGPT OAuth, Amazon Bedrock, generic OpenAI-compatible)

All old ``session:*`` socket events and runners REST endpoints stay
untouched (M8 removes them).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema
from pydantic import Field

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.harness.enums import ProviderType
from apps.harness.provider_accounts import SUPPORTED_PROVIDERS as _SUPPORTED_PROVIDERS
from apps.harness.providers.openrouter import DEFAULT_BASE_URL
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .constants import DEFAULT_MAX_DEPTH, MAX_SUBAGENT_DEPTH
from .timeline import (
    TIMELINE_TEXT_PART_TYPES,
    project_question_definitions,
    timeline_part_payload,
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


def _non_empty(value: str) -> str | None:
    """Return the stripped string or None when blank (edit no-op)."""
    stripped = (value or "").strip()
    return stripped or None


def _require_claude_session_owner(session, user_id: object) -> None:
    """Prevent another workspace collaborator from using personal Claude auth."""
    if getattr(session, "harness_id", "native") == "claude" and str(
        getattr(session, "created_by_id", "")
    ) != str(user_id):
        raise AuthenticationError(
            "Only the session owner may use its Claude connection"
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


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HarnessSessionCreateIn(Schema):
    """Request schema for starting a harness session."""

    prompt: str
    agent_name: str = "build"
    harness_id: str = "native"
    connection_id: uuid.UUID | None = None
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
    harness_id: str = "native"
    model: str = ""
    reasoning_effort: str = ""
    unread: bool = False
    manual_unread: bool = False
    needs_attention: bool = False
    attention_kind: str = ""
    updated_at: datetime
    last_message_at: datetime


class HarnessMessageIn(Schema):
    """Request schema for a follow-up prompt on a session."""

    prompt: str
    mode: str = ""
    model: str = ""
    reasoning_effort: str = ""
    skill_ids: list[str] = []


class HarnessSteerIn(Schema):
    """Request schema for a message sent into a running turn."""

    prompt: str


class HarnessSteerOut(Schema):
    """A queued (or withdrawn) message addressed to a running turn."""

    id: uuid.UUID
    content: str
    created_at: datetime


class HarnessForkIn(Schema):
    """Request schema for forking a harness session."""

    message_id: uuid.UUID | None = None
    include_message: bool = False


class HarnessMessageEditIn(Schema):
    """Request schema for editing a user message and rerunning."""

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
    resumed: bool = True


class HarnessSessionOut(Schema):
    """Response schema for a harness session."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title: str
    summary: str = ""
    mode: str
    agent_name: str
    harness_id: str = "native"
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
    harness_id: str = "native"
    role: str
    content: str
    model: str
    reasoning_effort: str = ""
    cost: float
    tokens: dict = {}
    finish: str
    error: str
    skill_ids: list[str] = []
    notice_dismissed_at: datetime | None = None
    position: int = 0
    message_position: int = 0
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
    position: int = 0
    message_position: int = 0


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


class RecentModelOut(Schema):
    """One recently used composer model with its last-used effort."""

    model: str
    effort: str = ""
    last_used_at: datetime


class RecentModelSaveIn(Schema):
    """Record one model usage (called on successful send)."""

    model: str
    effort: str = ""


class SubagentConfigIn(Schema):
    """Request schema for org-wide subagent depth."""

    max_depth: int = Field(
        default=DEFAULT_MAX_DEPTH, strict=True, ge=1, le=MAX_SUBAGENT_DEPTH
    )


class SubagentConfigOut(Schema):
    """Response schema for org-wide subagent depth."""

    max_depth: int = DEFAULT_MAX_DEPTH


class AgentSConfigIn(Schema):
    """Request schema for saving the org-wide Agent-S harness config."""

    grounding_model: str = ""
    grounding_width: int = 1920
    grounding_height: int = 1080
    model_temperature: float | None = None
    max_steps: int = 15
    max_trajectory_length: int = 8
    enable_reflection: bool = True
    enable_code_agent: bool = True
    enable_recording: bool = False
    screenshot_max_dimension: int = 2400
    action_pre_delay: float = 1.0
    action_post_delay: float = 1.0
    wait_delay: float = 5.0


class AgentSConfigOut(Schema):
    """Response schema for the org-wide Agent-S harness config."""

    grounding_model: str = ""
    grounding_width: int = 1920
    grounding_height: int = 1080
    model_temperature: float | None = None
    max_steps: int = 15
    max_trajectory_length: int = 8
    enable_reflection: bool = True
    enable_code_agent: bool = True
    enable_recording: bool = False
    screenshot_max_dimension: int = 2400
    action_pre_delay: float = 1.0
    action_post_delay: float = 1.0
    wait_delay: float = 5.0


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
    models: list[str] = []


class OpenRouterConnectionIn(Schema):
    """Upsert payload for an OpenRouter connection."""

    api_key: str = ""
    base_url: str = ""


class OpenAICompatibleConnectionIn(Schema):
    """Upsert payload for a generic OpenAI-compatible connection."""

    api_key: str = ""
    base_url: str = ""
    models: list[str] = []


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
    models: list[str] = []
    auth_method: str = ""
    region: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    bearer_token: str = ""


class ProviderAccountOut(Schema):
    """Safe metadata for one provider account."""

    id: str
    provider: str
    label: str
    scope: str
    connected: bool
    is_active: bool
    can_manage: bool
    base_url: str = ""
    api_key_hint: str = ""
    account_id: str = ""
    region: str = ""
    auth_method: str = ""
    models: list[str] = []
    auth_type: str = ""


class ProviderAccountIn(Schema):
    """Create/update payload for a named provider account."""

    label: str = ""
    shared: bool = False
    api_key: str = ""
    base_url: str = ""
    models: list[str] = []
    auth_method: str = ""
    region: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    bearer_token: str = ""
    token: str = ""
    auth_type: str = "api_token"


class ProviderSelectionIn(Schema):
    """Explicit active-account choice, including an empty selection."""

    account_id: uuid.UUID | None = Field(...)


class ChatGPTOAuthStartIn(Schema):
    """Optional account identity for a new or reconnecting ChatGPT flow."""

    label: str = ""
    shared: bool | None = None
    account_id: uuid.UUID | None = None


class ChatGPTOAuthCancelIn(Schema):
    """Optional pending OAuth flow identifier for cancellation."""

    flow_id: uuid.UUID | None = None


class ChatGPTOAuthStartOut(Schema):
    """Response for starting the ChatGPT device OAuth flow."""

    flow_id: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


class ChatGPTOAuthStatusOut(Schema):
    """Response for polling ChatGPT device OAuth status."""

    status: str
    account_id: str = ""


_VALID_PROVIDERS = set(_SUPPORTED_PROVIDERS)


def _secret_hint(secret: str) -> str:
    """Return a masked hint (last four chars) for a stored secret."""
    if not secret:
        return ""
    return f"••••{secret[-4:]}" if len(secret) > 4 else "••••"


def _openrouter_connection_view(
    org_id: uuid.UUID, user_id: int
) -> tuple[bool, str, str]:
    """Return active user's OpenRouter status without exposing other accounts."""
    from apps.harness.provider_accounts import ProviderAccountService

    account = (
        ProviderAccountService()
        .selected_accounts(org_id, user_id)
        .get(ProviderType.OPENROUTER)
    )
    if account is None:
        return False, "", DEFAULT_BASE_URL
    credentials = ProviderAccountService().get_credentials(account)
    api_key = str(credentials.get("api_key", "") or "")
    has_key = bool(api_key.strip())
    base_url = str((account.config or {}).get("base_url") or DEFAULT_BASE_URL)
    return has_key, _secret_hint(api_key) if has_key else "", base_url


def _provider_config_to_out(
    config, org_id: uuid.UUID, user_id: int
) -> ProviderConfigOut:  # type: ignore[no-untyped-def]
    """Map a ProviderConfig ORM row to ProviderConfigOut (never plaintext)."""
    has_key, hint, base_url = _openrouter_connection_view(org_id, user_id)
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


def _list_org_provider_connections(
    org_id: uuid.UUID, user_id: int
) -> list[ProviderConnectionOut]:
    """Return status for each provider based on this user's active account."""
    from apps.harness.provider_accounts import (
        SUPPORTED_PROVIDERS,
        ProviderAccountService,
    )

    accounts = ProviderAccountService()
    selected = accounts.selected_accounts(org_id, user_id)
    rows: list[ProviderConnectionOut] = []
    for provider in SUPPORTED_PROVIDERS:
        account = selected.get(provider)
        if account is None:
            rows.append(ProviderConnectionOut(provider=provider, connected=False))
        else:
            view = next(
                row
                for row in accounts.list_accounts(org_id, user_id, provider)
                if row["id"] == str(account.id)
            )
            rows.append(
                ProviderConnectionOut(
                    provider=provider,
                    connected=bool(view["connected"]),
                    base_url=view["base_url"],
                    api_key_hint=view["api_key_hint"],
                    account_id=view["account_id"],
                    region=view["region"],
                    auth_method=view["auth_method"],
                    models=(
                        _safe_manual_models(view["models"])
                        if provider == ProviderType.OPENAI_COMPATIBLE
                        else view["models"]
                    ),
                )
            )
    return rows


def _safe_manual_models(raw_models: list[str]) -> list[str]:
    """Normalize old dirty model lists without failing status reads."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in raw_models:
        model = str(raw or "").strip()
        if model and model not in seen:
            seen.add(model)
            result.append(model)
    return result


def _active_account(org_id: uuid.UUID, user_id: int, provider: str):
    """Return only the current user's explicitly active visible provider account."""
    from apps.harness.provider_accounts import ProviderAccountService

    return ProviderAccountService().selected_accounts(org_id, user_id).get(provider)


def _save_legacy_provider_account(
    org_id: uuid.UUID, user, provider: str, payload: dict[str, Any]
):
    """Compatibility upsert: active account only, else create private."""
    from apps.harness.provider_accounts import ProviderAccountService
    from apps.harness.providers.models_catalog import clear_models_cache

    accounts = ProviderAccountService()
    user_id = int(user.id)
    account = _active_account(org_id, user_id, provider)
    if account is not None and account.user_id is None:
        membership = accounts._membership(org_id, user_id)
        if membership.role != "admin":
            raise AuthenticationError(
                "Only org admins can manage shared provider accounts"
            )
    if account is None:
        existing_labels = {
            row["label"].casefold()
            for row in accounts.list_accounts(org_id, user_id, provider)
        }
        base_label = provider.replace("-", " ").title()
        label = base_label
        suffix = 2
        while label.casefold() in existing_labels:
            label = f"{base_label} {suffix}"
            suffix += 1
        payload = {**payload, "label": label, "shared": False}
    else:
        payload = {
            **payload,
            "label": account.label,
            "shared": account.user_id is None,
        }
    updated = accounts.save_account(
        organization_id=org_id,
        user=user,
        provider=provider,
        payload=payload,
        account_id=account.id if account is not None else None,
    )
    clear_models_cache(str(org_id))
    return updated


def _delete_org_provider_connection(
    org_id: uuid.UUID, user_id: int, provider: str
) -> None:
    """Delete only the caller's active account, never an arbitrary org row."""
    from apps.harness.provider_accounts import ProviderAccountService
    from apps.harness.providers.models_catalog import clear_models_cache

    account = _active_account(org_id, user_id, provider)
    if account is None:
        raise NotFoundError("ProviderAccount", provider)
    ProviderAccountService().delete_account(org_id, user_id, account.id)
    clear_models_cache(str(org_id))


def _validate_openai_compatible_models(models: list[str]) -> list[str]:
    """Validate and deduplicate manual model ids (deprecated shim).

    Prefer :meth:`ProviderConfigService.normalize_openai_compatible_models`;
    kept for backward-compatible imports. Empty ids after trim raise;
    an empty total list is allowed.
    """
    from apps.harness.services import ProviderConfigService

    return ProviderConfigService.normalize_openai_compatible_models(list(models or []))


def _validate_base_url(base_url: str) -> str:
    """Validate an http/https base URL (deprecated shim).

    Prefer :meth:`ProviderConfigService.validate_openai_compatible_base_url`;
    kept for backward-compatible imports.
    """
    from apps.harness.services import ProviderConfigService

    return ProviderConfigService.validate_openai_compatible_base_url(base_url)


def _agent_s_config_to_out(row: dict) -> AgentSConfigOut:
    """Map an AgentSConfigService view to AgentSConfigOut."""
    return AgentSConfigOut(
        grounding_model=row.get("grounding_model") or "",
        grounding_width=int(row.get("grounding_width", 1920)),
        grounding_height=int(row.get("grounding_height", 1080)),
        model_temperature=row.get("model_temperature"),
        max_steps=int(row.get("max_steps", 15)),
        max_trajectory_length=int(row.get("max_trajectory_length", 8)),
        enable_reflection=bool(row.get("enable_reflection", True)),
        enable_code_agent=bool(row.get("enable_code_agent", True)),
        enable_recording=bool(row.get("enable_recording", False)),
        screenshot_max_dimension=int(row.get("screenshot_max_dimension", 2400)),
        action_pre_delay=float(row.get("action_pre_delay", 1.0)),
        action_post_delay=float(row.get("action_post_delay", 1.0)),
        wait_delay=float(row.get("wait_delay", 5.0)),
    )


def _fetch_org_agent_s_config(org_id: uuid.UUID) -> AgentSConfigOut:
    """Load the org Agent-S config (defaults when unstored)."""
    from apps.harness.services import AgentSConfigService

    return _agent_s_config_to_out(AgentSConfigService().get_or_default(org_id))


def _save_org_agent_s_config(
    org_id: uuid.UUID, payload: AgentSConfigIn
) -> AgentSConfigOut:
    """Validate and upsert the org Agent-S config (partial payloads merge)."""
    from apps.harness.services import AgentSConfigService

    row = AgentSConfigService().save_config(
        org_id, payload.model_dump(exclude_unset=True)
    )
    return _agent_s_config_to_out(row)


def _save_org_agent_s_config_partial(
    org_id: uuid.UUID, values: dict
) -> AgentSConfigOut:
    """Validate raw MCP args (partial dict) and upsert the Agent-S config."""
    from apps.harness.services import AgentSConfigService

    row = AgentSConfigService().save_config(org_id, dict(values or {}))
    return _agent_s_config_to_out(row)


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


def _fetch_org_provider_config(org_id: uuid.UUID, user_id: int) -> ProviderConfigOut:
    """Load the org provider config and map to the public response.

    Missing legacy rows are normal (defaults were never saved): return
    an empty config shape instead of 404 so old frontend calls and
    gating fetches stop spamming ``Not Found`` warnings.
    """
    from apps.harness.services import ProviderConfigService

    service = ProviderConfigService()
    try:
        config = service.get_config(org_id)
    except NotFoundError:
        return ProviderConfigOut(
            base_url=_openrouter_connection_view(org_id, user_id)[2],
            default_model="",
            small_model="",
            computer_use_model="",
            default_effort="",
            small_effort="",
            computer_use_effort="",
            has_api_key=_openrouter_connection_view(org_id, user_id)[0],
            api_key_hint=_openrouter_connection_view(org_id, user_id)[1],
        )
    return _provider_config_to_out(config, org_id, user_id)


def _list_org_provider_models(
    org_id: uuid.UUID, user_id: int
) -> list[ProviderModelOut]:
    """Fetch the caller-scoped merged provider catalog."""
    from apps.harness.services import ProviderConfigService

    models = ProviderConfigService().list_models(org_id, user_id=user_id)
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
    org_id: uuid.UUID, user, payload: ProviderConfigIn
) -> ProviderConfigOut:
    """Save org-wide defaults and route compatibility keys to active/private account."""
    from apps.harness.provider_accounts import ProviderAccountService
    from apps.harness.services import ProviderConfigService

    account_service = ProviderAccountService()
    active = account_service.selected_accounts(org_id, user.id).get("openrouter")
    base_url = (payload.base_url or "").strip() or str(
        ((active.config if active else {}) or {}).get("base_url") or DEFAULT_BASE_URL
    )
    if (payload.api_key or "").strip() or (
        active is not None
        and "base_url" in payload.model_fields_set
        and (payload.base_url or "").strip()
    ):
        _save_legacy_provider_account(
            org_id,
            user,
            "openrouter",
            {"api_key": payload.api_key, "base_url": base_url},
        )
    config = ProviderConfigService().save_config(
        organization_id=org_id,
        api_key="",
        base_url=DEFAULT_BASE_URL,
        default_model=payload.default_model or "",
        small_model=payload.small_model or "",
        computer_use_model=payload.computer_use_model or "",
        default_effort=payload.default_effort or "",
        small_effort=payload.small_effort or "",
        computer_use_effort=payload.computer_use_effort or "",
    )
    return _provider_config_to_out(config, org_id, int(user.id))


def _delete_org_provider_config(org_id: uuid.UUID) -> None:
    """Delete the org default-model config only (connections are kept).

    Provider credentials in ``ProviderConnection`` rows are intentionally
    left intact so deleting model defaults does not disconnect providers.
    """
    from apps.harness.services import ProviderConfigService

    ProviderConfigService().delete_config(org_id)


def _recent_model_to_out(row) -> RecentModelOut:  # type: ignore[no-untyped-def]
    """Map a RecentModel ORM row to its response schema."""
    return RecentModelOut(
        model=row.model or "",
        effort=row.effort or "",
        last_used_at=row.last_used_at,
    )


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
        harness_id=getattr(session, "harness_id", "native"),
        title=session.title or "",
        summary=session.summary or "",
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
        return 201, await sync_to_async(_session_to_out_with_unread)(service, fresh)
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
        return 202, await sync_to_async(_session_to_out_with_unread)(service, fresh)
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
        out = await sync_to_async(_session_to_out_with_unread)(service, forked)
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
        return 202, await sync_to_async(_session_to_out_with_unread)(service, fresh)
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
        return 200, await sync_to_async(_session_to_out_with_unread)(service, updated)
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
        return 200, await sync_to_async(_session_to_out_with_unread)(service, updated)
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
        return 200, await sync_to_async(_session_to_out_with_unread)(service, aborted)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ConflictError as exc:
        return 409, {"detail": exc.message, "code": exc.code}


def _harness_conversation_payload(
    request: HttpRequest, session_id: uuid.UUID, *, timeline: bool
):
    """Build legacy or lightweight conversation envelope after ownership checks."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _perm_denied(APIKeyPermission.HARNESS_READ)
    org_id = _get_org_id(request)
    OrganizationService().require_membership(request.user, org_id)
    try:
        service = _resolve_harness_service()
        session = service.get_session(session_id)
        _owned_workspace(request, org_id, session.workspace_id)
        messages = (
            service.list_timeline_messages(session.id)
            if timeline
            else service.list_messages(session.id)
        )
        parts = (
            service.list_timeline_parts(session.id)
            if timeline
            else service.list_parts(session.id)
        )
        message_positions = {
            str(message.id): int(getattr(message, "position", 0) or 0)
            for message in messages
        }
        parts_by_message: dict[str, list] = {}
        for part in parts:
            if timeline:
                part.message_position = message_positions.get(str(part.message_id), 0)
                payload = timeline_part_payload(
                    part,
                    text_output=getattr(part, "timeline_output", "")
                    if part.type in TIMELINE_TEXT_PART_TYPES
                    else None,
                )
            else:
                safe_meta = dict(part.meta or {})
                if part.type == "patch":
                    safe_meta.pop("old_content", None)
                    safe_meta.pop("new_content", None)
                payload = HarnessPartOut(
                    id=part.id,
                    message_id=part.message_id,
                    type=part.type,
                    state=part.state,
                    call_id=part.call_id or "",
                    title=part.title or "",
                    output=part.output or "",
                    input=dict(part.input or {}),
                    meta=safe_meta,
                    position=int(getattr(part, "position", 0) or 0),
                    message_position=message_positions.get(str(part.message_id), 0),
                ).model_dump(mode="json")
            parts_by_message.setdefault(str(part.message_id), []).append(payload)
        pending_questions = service.list_pending_questions(
            session.id, include_descendants=True
        )
        if timeline:
            pending_questions = [
                {
                    **question,
                    "questions": project_question_definitions(
                        question.get("questions")
                    ),
                }
                for question in pending_questions
            ]
        return 200, {
            "session": _session_to_out_with_unread(service, session).model_dump(
                mode="json"
            ),
            "messages": [
                {
                    **HarnessMessageOut(
                        id=message.id,
                        harness_id=getattr(message, "harness_id", "native"),
                        role=message.role,
                        # Timeline text comes from ordered text parts; only
                        # legacy assistant rows with no text parts need the
                        # message-level fallback. This prevents sending both.
                        content=(
                            (getattr(message, "timeline_content", "") or "")
                            if timeline
                            else (message.content or "")
                        ),
                        model=message.model or "",
                        reasoning_effort=message.reasoning_effort or "",
                        cost=float(message.cost or 0.0),
                        tokens=dict(message.tokens or {}),
                        finish=message.finish or "",
                        error=message.error or "",
                        skill_ids=[
                            str(skill_id) for skill_id in (message.skill_ids or [])
                        ],
                        notice_dismissed_at=message.notice_dismissed_at,
                        position=int(getattr(message, "position", 0) or 0),
                        message_position=int(getattr(message, "position", 0) or 0),
                        created_at=message.created_at,
                        completed_at=message.completed_at,
                    ).model_dump(mode="json"),
                    "parts": parts_by_message.get(str(message.id), []),
                }
                for message in messages
            ],
            "permissions": service.list_pending_permissions(
                session.id, include_descendants=True
            ),
            "questions": pending_questions,
            "steers": [
                entry.to_payload() for entry in service.list_pending_steers(session.id)
            ],
        }
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.get(
    "/harness/sessions/{session_id}/parts",
    response={200: dict, 403: dict, 404: dict},
    summary="List messages and full parts of a harness session (legacy)",
)
def list_harness_parts(request: HttpRequest, session_id: uuid.UUID):
    """Return the full legacy message/part envelope, excluding patch file copies."""
    return _harness_conversation_payload(request, session_id, timeline=False)


@harness_router.get(
    "/harness/sessions/{session_id}/timeline",
    response={200: dict, 403: dict, 404: dict},
    summary="Load all session messages with lightweight part projections",
)
def get_harness_timeline(request: HttpRequest, session_id: uuid.UUID):
    """Return the session envelope without heavyweight detail fields."""
    return _harness_conversation_payload(request, session_id, timeline=True)


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
        safe_meta = dict(part.meta or {})
        if part.type == "patch":
            safe_meta.pop("old_content", None)
            safe_meta.pop("new_content", None)
        display = dict(part.display or {})
        display.pop("_reasoning_line", None)
        return 200, {
            **HarnessPartOut(
                id=part.id,
                message_id=part.message_id,
                type=part.type,
                state=part.state,
                call_id=part.call_id or "",
                title=part.title or "",
                output=part.output or "",
                input=dict(part.input or {}),
                meta=safe_meta,
                position=int(getattr(part, "position", 0) or 0),
                message_position=int(getattr(part, "message_position", 0) or 0),
            ).model_dump(mode="json"),
            "display": display,
            "detail_loaded": True,
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
# Provider config endpoints (org-scoped; workspace paths are aliases)
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


def _fetch_org_subagent_config(org_id: uuid.UUID) -> SubagentConfigOut:
    """Return the organization's subagent config view."""
    from apps.harness.services import SubagentConfigService

    return SubagentConfigOut(**SubagentConfigService().get_or_default(org_id))


def _save_org_subagent_config(
    org_id: uuid.UUID, payload: SubagentConfigIn
) -> SubagentConfigOut:
    """Persist the organization's validated subagent config."""
    from apps.harness.services import SubagentConfigService

    return SubagentConfigOut(
        **SubagentConfigService().save_config(org_id, payload.max_depth)
    )


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
        return 200, _fetch_org_subagent_config(org_id)
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
        return 200, _save_org_subagent_config(org_id, payload)
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
        return 200, _fetch_org_agent_s_config(org_id)
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
        return 200, _save_org_agent_s_config(org_id, payload)
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
        return 200, _fetch_org_provider_config(org_id, int(request.user.id))
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
        return 200, _list_org_provider_models(org_id, int(request.user.id))
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
    response={200: ProviderConfigOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Save (upsert) the org-wide provider config",
)
def save_org_provider_config(request: HttpRequest, payload: ProviderConfigIn):
    """Upsert the org provider config via Fernet encryption."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        return 200, _save_org_provider_config(org_id, request.user, payload)
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
    """Return connection status for every supported provider."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _perm_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        org_id = _get_org_id(request)
        OrganizationService().require_membership(request.user, org_id)
        return 200, _list_org_provider_connections(org_id, int(request.user.id))
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@harness_router.put(
    "/provider-config/providers/{provider}/",
    response={200: ProviderConnectionOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Compatibility upsert for the active provider account",
)
def upsert_org_provider_connection(
    request: HttpRequest,
    provider: str,
    payload: ProviderConnectionUpsertIn,
):
    """Upsert only the active visible account or create a private account."""
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
        values = payload.model_dump(exclude_unset=True)
        if (
            provider_id == ProviderType.OPENAI_COMPATIBLE
            and "models" not in payload.model_fields_set
        ):
            values.pop("models", None)
        if (
            provider_id == ProviderType.OPENAI_COMPATIBLE
            and not values.get("base_url", "").strip()
        ):
            active = _active_account(org_id, int(request.user.id), provider_id)
            if active is not None:
                values["base_url"] = str((active.config or {}).get("base_url") or "")
                if "models" not in values:
                    values["models"] = list(
                        (active.config or {}).get("models", []) or []
                    )
        _save_legacy_provider_account(org_id, request.user, provider_id, values)
        rows = _list_org_provider_connections(org_id, int(request.user.id))
        return 200, next(row for row in rows if row.provider == provider_id)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
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
        _delete_org_provider_connection(org_id, int(request.user.id), provider_id)
        return 204, None
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


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
