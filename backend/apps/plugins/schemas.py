"""
Pydantic schemas for the plugins REST API.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ninja import Schema

# --- Nested component inputs ---


class PluginSkillIn(Schema):
    """Nested skill definition for plugin create/replace."""

    name: str
    slug: str = ""
    body: str
    position: int = 0


class PluginSkillOut(Schema):
    """A plugin skill fragment."""

    id: uuid.UUID
    name: str
    slug: str
    body: str
    position: int


class PluginMcpServerIn(Schema):
    """Nested MCP server definition for plugin create/replace."""

    name: str
    slug: str = ""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    cwd: str = "/workspace"
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    startup_timeout_seconds: int = 30
    request_timeout_seconds: int = 60


class PluginMcpServerOut(Schema):
    """An MCP server definition."""

    id: uuid.UUID
    name: str
    slug: str
    transport: str
    command: str
    args: list[str]
    cwd: str
    env: dict[str, str]
    url: str
    headers: dict[str, str]
    startup_timeout_seconds: int
    request_timeout_seconds: int


class PluginCredentialServiceIn(Schema):
    """Define (or reference) a credential service for a plugin requirement."""

    slug: str = ""
    name: str = ""
    description: str = ""
    credential_type: str = "env"
    env_var_name: str = ""
    target_path: str = ""
    label: str = ""
    # When set, reference an existing visible service instead of creating one.
    service_id: uuid.UUID | None = None


class PluginCredentialRequirementIn(Schema):
    """Nested credential requirement for plugin create/replace."""

    key: str
    description: str = ""
    required: bool = True
    credential_service: PluginCredentialServiceIn


class PluginCredentialRequirementOut(Schema):
    """A plugin-wide credential requirement (no secret values)."""

    id: uuid.UUID
    key: str
    description: str
    required: bool
    service_id: uuid.UUID
    service_name: str
    service_slug: str
    credential_type: str
    plugin_owned_service: bool


# --- Plugin inputs / outputs ---


class PluginCreateIn(Schema):
    """Payload for creating an org-owned plugin with nested components."""

    name: str
    slug: str = ""
    description: str = ""
    enabled: bool = True
    published: bool = True
    skills: list[PluginSkillIn] = []
    mcp_servers: list[PluginMcpServerIn] = []
    credential_requirements: list[PluginCredentialRequirementIn] = []


class PluginUpdateIn(Schema):
    """Payload for replacing plugin metadata + full component lists."""

    name: str | None = None
    slug: str | None = None
    description: str | None = None
    enabled: bool | None = None
    published: bool | None = None
    skills: list[PluginSkillIn] | None = None
    mcp_servers: list[PluginMcpServerIn] | None = None
    credential_requirements: list[PluginCredentialRequirementIn] | None = None


class PluginCredentialReadinessOut(Schema):
    """Credential readiness summary for the active org (no secret values)."""

    required_service_ids: list[uuid.UUID] = []
    missing_required_service_ids: list[uuid.UUID] = []
    ready: bool = True


class PluginOut(Schema):
    """A plugin with nested components and org activation state."""

    id: uuid.UUID
    name: str
    slug: str
    description: str
    enabled: bool
    published: bool
    organization_id: uuid.UUID | None = None
    is_global: bool = False
    org_enabled: bool = False
    skills: list[PluginSkillOut] = []
    mcp_servers: list[PluginMcpServerOut] = []
    credential_requirements: list[PluginCredentialRequirementOut] = []
    credential_readiness: PluginCredentialReadinessOut | None = None
    created_at: datetime
    updated_at: datetime


class PluginActivationIn(Schema):
    """Toggle org-wide activation of a plugin."""

    active: bool


class WorkspacePluginOut(Schema):
    """An org-enabled plugin with workspace activation + readiness state."""

    id: uuid.UUID
    name: str
    slug: str
    description: str
    organization_id: uuid.UUID | None = None
    is_global: bool = False
    workspace_enabled: bool = False
    missing_required_credentials: list[dict] = []
    ready: bool = True


class WorkspacePluginsUpdateIn(Schema):
    """Replace workspace plugin activations atomically."""

    plugin_ids: list[uuid.UUID] = []
