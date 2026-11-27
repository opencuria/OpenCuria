"""
Pydantic schemas for the plugins REST API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import ConfigDict, Field, model_serializer


class DesktopResource(Schema):
    """Runner-managed display activation policy."""

    model_config = ConfigDict(extra="forbid")
    activation: Literal["server_start", "first_tool"] = "server_start"


class PluginResources(Schema):
    """Declared runtime resources; omission means no managed desktop."""

    model_config = ConfigDict(extra="forbid")
    desktop: DesktopResource | None = None

    @model_serializer
    def serialize_resources(self) -> dict:
        """Absent resources serialize as {}, not a null desktop declaration."""
        return (
            {"desktop": self.desktop.model_dump()} if self.desktop is not None else {}
        )


# --- Nested component inputs ---


class McpToolPolicy(Schema):
    """Explicit model exposure and managed lifecycle for one MCP server."""

    model_config = ConfigDict(extra="forbid")
    exposed_tools: list[str] | None = None
    internal_tools: list[str] = Field(default_factory=list)
    desktop_tools: list[str] | None = None
    execution_tools: list[str] = Field(default_factory=list)
    omit_tools_from: list[Literal["code_mode", "deferred"]] = Field(
        default_factory=list
    )
    on_run_end: str = ""
    desktop_kind: Literal["mcp", "computeruse"] = "mcp"
    exclusive_input: bool = False


class RuntimeArtifact(Schema):
    """An allowlisted runner-provisioned executable, never an arbitrary URL."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)


class PluginSkillIn(Schema):
    """Nested skill definition for plugin create/replace.

    ``id`` keeps an existing skill (and chat selections of it) across
    renames. A body starting with SKILL.md frontmatter may fill an empty
    name or description.
    """

    id: uuid.UUID | None = None
    name: str = ""
    slug: str = ""
    description: str = ""
    body: str
    position: int = 0


class PluginSkillOut(Schema):
    """A skill of a plugin."""

    id: uuid.UUID
    name: str
    slug: str
    description: str = ""
    body: str
    position: int


class SkillOut(Schema):
    """A skill the user can select for a chat or scheduled task."""

    id: uuid.UUID
    name: str
    description: str
    plugin_id: uuid.UUID
    plugin_name: str
    scope: Literal["personal", "organization", "builtin"]


class PluginMcpServerIn(Schema):
    """Nested MCP server definition for plugin create/replace.

    ``command`` may be a whole command line (``npx -y @acme/mcp``) when
    ``args`` is empty. Secret values are referenced as
    ``{{credential.KEY}}`` of a credential requirement. Timeouts are not
    configurable.
    """

    id: uuid.UUID | None = None
    name: str
    slug: str = ""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    cwd: str = "/workspace"
    resources: PluginResources = Field(default_factory=PluginResources)
    tool_policy: McpToolPolicy = Field(default_factory=McpToolPolicy)
    runtime_artifact: RuntimeArtifact | None = None
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    auth_type: str = "none"
    oauth_requirement_key: str = ""


class PluginMcpServerOut(Schema):
    """An MCP server definition."""

    id: uuid.UUID
    name: str
    slug: str
    transport: str
    command: str
    args: list[str]
    cwd: str
    resources: PluginResources = Field(default_factory=PluginResources)
    tool_policy: McpToolPolicy = Field(default_factory=McpToolPolicy)
    runtime_artifact: RuntimeArtifact | None = None
    env: dict[str, str]
    url: str
    headers: dict[str, str]
    auth_type: str = "none"
    oauth_requirement_key: str = ""


class NewCredentialServiceIn(Schema):
    """A secret service created with the plugin (personal or organization)."""

    name: str
    env_var_name: str = ""


class PluginCredentialRequirementIn(Schema):
    """A credential service the plugin needs, optionally with its credential.

    Name an existing service (``service_id``) or create one
    (``new_service``). ``credential_id`` links the credential the plugin
    always uses; ``value`` stores a new value as that credential (or
    updates the linked one). Without either, each workspace attaches its
    own credential of the service.
    """

    key: str
    description: str = ""
    required: bool = True
    service_id: uuid.UUID | None = None
    new_service: NewCredentialServiceIn | None = None
    credential_id: uuid.UUID | None = None
    value: str | None = None


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
    credential_id: uuid.UUID | None = None
    credential_name: str = ""
    credential_scope: str = ""


# --- Plugin inputs / outputs ---


class PluginCreateIn(Schema):
    """Payload for creating a personal or organization plugin."""

    name: str
    slug: str = ""
    scope: Literal["personal", "organization"] = "personal"
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


class PluginOut(Schema):
    """A plugin with nested components and availability.

    ``available``: can be enabled in the caller's workspaces (personal
    plugins always; others once released for the organization).
    ``editable``: the caller may change or delete it.
    """

    id: uuid.UUID
    name: str
    slug: str
    description: str
    enabled: bool
    published: bool
    organization_id: uuid.UUID | None = None
    scope: Literal["personal", "organization", "builtin"] = "builtin"
    is_global: bool = False
    org_enabled: bool = False
    available: bool = False
    editable: bool = False
    skills: list[PluginSkillOut] = []
    mcp_servers: list[PluginMcpServerOut] = []
    credential_requirements: list[PluginCredentialRequirementOut] = []
    created_at: datetime
    updated_at: datetime


class PluginActivationIn(Schema):
    """Toggle org-wide activation of a plugin."""

    active: bool
