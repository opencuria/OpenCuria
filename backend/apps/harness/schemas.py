"""
Request/response schemas and DTO mappers for the harness adapters.

Shared by the REST routers (``api.py``) and the MCP tools
(``apps.mcp_app``) so both expose identical payloads without importing
each other. Pure mapping only: no business rules, no ORM queries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ninja import Schema
from pydantic import Field

from .constants import DEFAULT_MAX_DEPTH, MAX_SUBAGENT_DEPTH

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
    """Org-wide model and reasoning-effort defaults; accounts hold credentials."""

    default_model: str = ""
    small_model: str = ""
    computer_use_model: str = ""
    default_effort: str = ""
    small_effort: str = ""
    computer_use_effort: str = ""


class ProviderConfigOut(ProviderConfigIn):
    """Saved org-wide model and reasoning-effort defaults."""


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


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def provider_config_to_out(config) -> ProviderConfigOut:  # type: ignore[no-untyped-def]
    """Map stored model/effort defaults (empty when unconfigured) to the response."""
    if config is None:
        return ProviderConfigOut()
    return ProviderConfigOut(
        **{name: getattr(config, name) or "" for name in ProviderConfigOut.model_fields}
    )


def provider_model_to_out(model) -> ProviderModelOut:  # type: ignore[no-untyped-def]
    """Map a catalog ``ProviderModel`` to its response schema."""
    return ProviderModelOut(
        id=model.id,
        name=model.name,
        provider=model.provider,
        reasoning_efforts=list(model.reasoning_efforts),
        default_effort=model.default_effort,
        supports_tools=model.supports_tools,
        context_length=model.context_length,
        max_output_tokens=model.max_output_tokens,
    )


def agent_s_config_to_out(row: dict) -> AgentSConfigOut:
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


def session_to_out(
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


def session_to_out_with_unread(service, session) -> HarnessSessionOut:  # type: ignore[no-untyped-def]
    """Map a session and compute its unread and attention flags."""
    attention = service.attention_for_sessions([session]).get(session.id, {})
    return session_to_out(
        session,
        unread=service.is_session_unread(session),
        manual_unread=session.manual_unread_at is not None,
        needs_attention=attention.get("needs_attention", False),
        attention_kind=attention.get("attention_kind", ""),
    )
