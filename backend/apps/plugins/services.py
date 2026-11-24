"""
Service layer for the plugins app.

All business logic lives here. The API layer delegates to these methods;
repositories encapsulate all ORM access.
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections import defaultdict
from typing import Any

import structlog
from django.db import transaction
from django.utils.text import slugify

from apps.credentials.oauth_metadata import validate_oauth_server_url
from apps.credentials.services import CredentialServiceSvc, CredentialSvc
from common.exceptions import ConflictError, NotFoundError, ServiceError

from .credential_links import usable_credential
from .models import (
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    PluginSkill,
    PluginTransport,
)
from .repositories import (
    OrgPluginActivationRepository,
    PluginCredentialRequirementRepository,
    PluginMcpServerRepository,
    PluginRepository,
    PluginSkillRepository,
    WorkspacePluginActivationRepository,
)
from .skill_documents import (
    MAX_DESCRIPTION_LEN,
    default_description,
    parse_skill_markdown,
)

logger = structlog.get_logger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY_RE = re.compile(r"^[a-z0-9_]+(?:[a-z0-9_-]*[a-z0-9_])?$", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}")
_MAX_NAME_LEN = 255
_MAX_SLUG_LEN = 255
_MAX_BODY_LEN = 200_000
_MAX_COMMAND_LEN = 1024
_MAX_ARG_LEN = 1024
_MAX_ARGS_COUNT = 64
_MAX_MAPPING_ENTRIES = 64
_MAX_MAPPING_KEY_LEN = 256
_MAX_MAPPING_VALUE_LEN = 4096
_MAX_URL_LEN = 2048

#: Timeouts of user-defined MCP servers; users never configure them. Startup
#: covers a cold ``npx``/``uvx`` download inside the workspace; requests may
#: run long (browsers, builds) and the user can stop a run at any time.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 600

SCOPE_PERSONAL = "personal"
SCOPE_ORGANIZATION = "organization"
SCOPE_BUILTIN = "builtin"

PLAYWRIGHT_PLUGIN_SLUG = "playwright"
#: Headed Chromium on the shared workspace desktop (KasmVNC :1) so the
#: browser is visible in the desktop viewer. ``--headless`` must stay
#: out: headed is the playwright-mcp default and needs DISPLAY.
PLAYWRIGHT_MCP_ARGS = [
    "-y",
    "@playwright/mcp@latest",
    "--isolated",
    "--no-sandbox",
    "--executable-path",
    "/usr/bin/google-chrome-stable",
    "--output-dir",
    "/workspace/.opencuria/playwright",
]
#: Windows guests drive Microsoft Edge; the Linux Chrome path and sandbox
#: flag do not apply there.
PLAYWRIGHT_MCP_WINDOWS_ARGS = [
    "-y",
    "@playwright/mcp@latest",
    "--isolated",
    "--browser",
    "msedge",
    "--output-dir",
    "/workspace/.opencuria/playwright",
]
#: Display bindings are supplied by the runner, never static environment.
PLAYWRIGHT_MCP_ENV = {}
PLAYWRIGHT_MCP_RESOURCES = {"desktop": {"activation": "first_tool"}}

#: Seeded global plugin exposing OpenCuria's own MCP server to agents.
OPENCURIA_PLUGIN_ID = uuid.UUID("128eb61c-0bc6-4c64-8a1b-2c9dc4e440b6")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def normalize_slug(value: str, *, field: str = "slug") -> str:
    """Normalize and validate a URL-safe slug."""
    normalized = slugify((value or "").strip())
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > _MAX_SLUG_LEN:
        raise ValueError(f"{field} must be at most {_MAX_SLUG_LEN} characters")
    if not _SLUG_RE.match(normalized):
        raise ValueError(f"{field} must be URL-safe (lowercase, digits, dashes)")
    return normalized


def normalize_requirement_key(value: str) -> str:
    """Validate a stable credential requirement key."""
    key = (value or "").strip()
    if not key:
        raise ValueError("Credential requirement key must not be empty")
    if len(key) > _MAX_SLUG_LEN:
        raise ValueError("Credential requirement key is too long")
    if not _KEY_RE.match(key):
        raise ValueError(
            "Credential requirement key must be a stable identifier "
            "(letters, digits, dashes, underscores)"
        )
    return key


def validate_name(value: str, *, field: str = "Name") -> str:
    """Validate a human-readable name."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > _MAX_NAME_LEN:
        raise ValueError(f"{field} must be at most {_MAX_NAME_LEN} characters")
    return cleaned


def validate_placeholder_mapping(mapping: dict, *, field: str) -> dict[str, str]:
    """Validate env/header mappings (literals or {{credential.KEY}})."""
    if not isinstance(mapping, dict):
        raise ValueError(f"{field} must be a mapping")
    if len(mapping) > _MAX_MAPPING_ENTRIES:
        raise ValueError(f"{field} must have at most {_MAX_MAPPING_ENTRIES} entries")
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in mapping.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ValueError(f"{field} keys and values must be strings")
        key = raw_key.strip()
        if not key or len(key) > _MAX_MAPPING_KEY_LEN:
            raise ValueError(f"{field} keys must be 1..{_MAX_MAPPING_KEY_LEN} chars")
        if len(raw_value) > _MAX_MAPPING_VALUE_LEN:
            raise ValueError(
                f"{field} value for '{key}' exceeds {_MAX_MAPPING_VALUE_LEN} characters"
            )
        # Values may be literals or contain {{credential.KEY}} placeholders;
        # reject other template syntax. A cheap guard: balanced braces only
        # appear via the credential placeholder pattern.
        stripped = _PLACEHOLDER_RE.sub("", raw_value)
        if "{{" in stripped or "}}" in stripped:
            raise ValueError(
                f"{field} value for '{key}' contains an unsupported placeholder "
                "(only {{credential.KEY}} is allowed)"
            )
        cleaned[key] = raw_value
    return cleaned


def normalize_skill_payload(item: dict, index: int) -> dict:
    """Validate a nested skill payload.

    A body that starts with SKILL.md frontmatter fills an empty name or
    description from it; the frontmatter itself is not stored.
    """
    raw_body = item.get("body") or ""
    if not isinstance(raw_body, str):
        raise ValueError("Skill body must be text")
    document = parse_skill_markdown(raw_body)
    name = validate_name(
        (item.get("name") or "").strip() or document.name, field="Skill name"
    )
    slug = normalize_slug(item.get("slug") or name, field="Skill slug")
    body = document.body
    if not body.strip():
        raise ValueError("Skill body must not be empty")
    if len(body) > _MAX_BODY_LEN:
        raise ValueError(f"Skill body must be at most {_MAX_BODY_LEN} characters")
    description = item.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("Skill description must be text")
    description = " ".join(
        ((description or "").strip() or document.description).split()
    ) or default_description(body)
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"Skill description must be at most {MAX_DESCRIPTION_LEN} characters"
        )
    try:
        position = int(item.get("position", index))
    except (TypeError, ValueError):
        raise ValueError("Skill position must be an integer") from None
    if position < 0 or position > 10_000:
        raise ValueError("Skill position must be between 0 and 10000")
    return {
        "id": item.get("id") or None,
        "name": name,
        "slug": slug,
        "description": description,
        "body": body,
        "position": position,
    }


def split_command_line(command: str, args: list[str]) -> tuple[str, list[str]]:
    """Split ``npx -y @acme/mcp`` into executable and arguments (no shell).

    A command without whitespace is returned unchanged. A command line plus
    separate ``args`` is ambiguous and rejected.
    """
    if not re.search(r"\s", command):
        return command, args
    if args:
        raise ValueError(
            "Put the whole command line into command, or the executable into "
            "command and its arguments into args"
        )
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"MCP command line cannot be parsed: {exc}") from None
    if not parts:
        raise ValueError("MCP command is required for stdio transport")
    return parts[0], parts[1:]


def normalize_mcp_payload(item: dict) -> dict:
    """Validate a nested MCP server payload."""
    name = validate_name(item.get("name", ""), field="MCP server name")
    slug = normalize_slug(
        item.get("slug") or item.get("name", ""), field="MCP server slug"
    )
    transport = (item.get("transport") or "stdio").strip()
    # ``builtin`` is reserved for the seeded OpenCuria plugin.
    valid_transports = {
        c.value for c in PluginTransport if c is not PluginTransport.BUILTIN
    }
    if transport not in valid_transports:
        raise ValueError(f"MCP transport must be one of {sorted(valid_transports)}")

    command = item.get("command") or ""
    if not isinstance(command, str):
        raise ValueError("MCP command must be a string")
    command = command.strip()
    args = item.get("args") or []
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("MCP args must be a list of strings")
    if transport == PluginTransport.STDIO and command:
        command, args = split_command_line(command, list(args))
    if len(args) > _MAX_ARGS_COUNT:
        raise ValueError(f"MCP args must have at most {_MAX_ARGS_COUNT} entries")
    for arg in args:
        if len(arg) > _MAX_ARG_LEN:
            raise ValueError("MCP arg exceeds maximum length")
        if "\x00" in arg:
            raise ValueError("MCP args must not contain null bytes")

    auth_type = (item.get("auth_type") or "none").strip().lower()
    if auth_type not in {"none", "oauth"}:
        raise ValueError("MCP auth_type must be 'none' or 'oauth'")
    oauth_requirement_key = (item.get("oauth_requirement_key") or "").strip()
    if auth_type == "oauth":
        if transport == PluginTransport.STDIO:
            raise ValueError("OAuth is only supported for HTTP MCP servers")
        if not oauth_requirement_key:
            raise ValueError("OAuth MCP servers require oauth_requirement_key")
        if "authorization" in {str(k).lower() for k in (item.get("headers") or {})}:
            raise ValueError("OAuth Authorization header is managed by the server")
    elif oauth_requirement_key:
        raise ValueError("oauth_requirement_key requires auth_type='oauth'")

    url = item.get("url") or ""
    if not isinstance(url, str):
        raise ValueError("MCP url must be a string")
    url = url.strip()
    cwd = item.get("cwd") or "/workspace"
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("MCP cwd must not be empty")
    cwd = cwd.strip()
    if len(cwd) > _MAX_COMMAND_LEN or "\x00" in cwd:
        raise ValueError("MCP cwd is invalid")
    from .schemas import McpToolPolicy, PluginResources, RuntimeArtifact

    resources = PluginResources.model_validate(item.get("resources", {}))
    policy = McpToolPolicy.model_validate(item.get("tool_policy") or {})
    artifact = (
        RuntimeArtifact.model_validate(item["runtime_artifact"])
        if item.get("runtime_artifact")
        else None
    )
    if artifact and transport != PluginTransport.STDIO:
        raise ValueError("Runtime artifacts require stdio transport")
    if policy.on_run_end and policy.on_run_end not in policy.internal_tools:
        raise ValueError("The lifecycle tool must be declared internal")
    if set(policy.internal_tools) & set(policy.exposed_tools or []):
        raise ValueError("Internal tools cannot be exposed to the model")
    if policy.exclusive_input and resources.desktop is None:
        raise ValueError("Exclusive input requires a managed desktop")
    if policy.desktop_kind == "computeruse" and resources.desktop is None:
        raise ValueError("Computer-use tools require a managed desktop")
    env = validate_placeholder_mapping(item.get("env") or {}, field="MCP env")
    if resources.desktop is not None:
        if transport != PluginTransport.STDIO:
            raise ValueError("Managed desktop resources require stdio transport")
        if {"DISPLAY", "XAUTHORITY"} & env.keys():
            raise ValueError(
                "Managed desktop supplies DISPLAY and XAUTHORITY; remove these "
                "env entries or disable the managed desktop resource"
            )
    headers = validate_placeholder_mapping(
        item.get("headers") or {}, field="MCP headers"
    )

    try:
        startup_timeout = int(
            item.get("startup_timeout_seconds") or DEFAULT_STARTUP_TIMEOUT_SECONDS
        )
        request_timeout = int(
            item.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError):
        raise ValueError("MCP timeouts must be integers") from None
    if not 1 <= startup_timeout <= 600:
        raise ValueError("startup_timeout_seconds must be between 1 and 600")
    if not 1 <= request_timeout <= 600:
        raise ValueError("request_timeout_seconds must be between 1 and 600")

    if transport == PluginTransport.STDIO:
        if not command:
            raise ValueError("MCP command is required for stdio transport")
        if len(command) > _MAX_COMMAND_LEN:
            raise ValueError("MCP command exceeds maximum length")
        if any(c in command for c in (" ", "\x00", "\n", ";", "|", "&", "$", "`")):
            raise ValueError(
                "MCP command must be a single executable "
                "(no shell metacharacters or arguments)"
            )
        if url:
            raise ValueError("MCP url must be empty for stdio transport")
    else:
        if not url or not re.match(r"^https?://", url, re.IGNORECASE):
            raise ValueError("MCP url must be an http(s) URL for http/sse transports")
        if len(url) > _MAX_URL_LEN:
            raise ValueError("MCP url exceeds maximum length")
        if command or args:
            raise ValueError("MCP command/args must be empty for http/sse transports")
        if auth_type == "oauth":
            url = validate_oauth_server_url(url)

    return {
        "id": item.get("id") or None,
        "name": name,
        "slug": slug,
        "transport": transport,
        "command": command,
        "args": list(args),
        "cwd": cwd,
        "env": env,
        "resources": resources.model_dump(exclude_none=True),
        "tool_policy": policy.model_dump(exclude_defaults=True),
        "runtime_artifact": artifact.model_dump() if artifact else {},
        "url": url,
        "headers": headers,
        "auth_type": auth_type,
        "oauth_requirement_key": oauth_requirement_key,
        "startup_timeout_seconds": startup_timeout,
        "request_timeout_seconds": request_timeout,
    }


# ---------------------------------------------------------------------------
# Plugin service
# ---------------------------------------------------------------------------


class PluginSelectionError(ConflictError):
    """Workspace plugin selection conflict with safe missing-credential details."""

    def __init__(self, message: str, gaps: list[dict]) -> None:
        super().__init__(message, code="missing_plugin_credentials")
        self.gaps = gaps


class PluginPermissionError(ServiceError):
    """The caller may see the plugin but not change it (HTTP 403)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="forbidden")


class PluginService:
    """Business logic for plugins and their activations.

    Visibility: built-in plugins, the organization's plugins and the
    caller's own personal plugins. Built-in and organization plugins are
    *available* in workspaces once released for the organization (admin
    setting); personal plugins are always available to their owner.
    """

    def __init__(self) -> None:
        self.plugins = PluginRepository
        self.skills = PluginSkillRepository
        self.mcp_servers = PluginMcpServerRepository
        self.requirements = PluginCredentialRequirementRepository
        self.org_activations = OrgPluginActivationRepository
        self.workspace_activations = WorkspacePluginActivationRepository

    # -- Queries ------------------------------------------------------------

    def list_visible(
        self, *, org_id: uuid.UUID, user: Any = None, admin: bool = True
    ) -> list[dict]:
        """Return the plugins the caller sees, serialized for them.

        Members (``admin=False``) see built-in and organization plugins only
        once they are released for the organization, and none of the MCP
        server configuration admins maintain. Everyone sees their own
        personal plugins in full.
        """
        user_id = getattr(user, "id", None)
        plugins = list(self.plugins.list_visible(org_id, user_id))
        payloads = self._serialize_plugins(
            plugins, org_id=org_id, user_id=user_id, admin=admin
        )
        if admin:
            return payloads
        return [
            payload
            if payload["scope"] == SCOPE_PERSONAL
            else self._member_view(payload)
            for payload in payloads
            if payload["available"]
        ]

    @staticmethod
    def _member_view(payload: dict) -> dict:
        """Keep what members use a plugin for; blank its server configuration."""
        return {
            **payload,
            "mcp_servers": [
                {
                    **server,
                    "command": "",
                    "args": [],
                    "cwd": "",
                    "env": {},
                    "resources": {},
                    "tool_policy": {},
                    "runtime_artifact": None,
                    "url": "",
                    "headers": {},
                    "oauth_requirement_key": "",
                }
                for server in payload["mcp_servers"]
            ],
        }

    def get_visible_model(
        self, plugin_id: uuid.UUID, *, org_id: uuid.UUID, user: Any = None
    ) -> Plugin:
        """Return a plugin the caller sees or raise NotFoundError."""
        plugin = self.plugins.get_visible_by_id(
            plugin_id, org_id, getattr(user, "id", None)
        )
        if plugin is None:
            raise NotFoundError("Plugin", str(plugin_id))
        return plugin

    def get_visible(
        self, plugin_id: uuid.UUID, *, org_id: uuid.UUID, user: Any, admin: bool
    ) -> dict:
        """Return one plugin serialized for the caller."""
        plugin = self.get_visible_model(plugin_id, org_id=org_id, user=user)
        payload = self._serialize_plugins(
            [plugin], org_id=org_id, user_id=getattr(user, "id", None), admin=admin
        )[0]
        if admin or payload["scope"] == SCOPE_PERSONAL:
            return payload
        if not payload["available"]:
            raise NotFoundError("Plugin", str(plugin_id))
        return self._member_view(payload)

    def workspace_plugin_ids(self, workspace_id: uuid.UUID) -> set[uuid.UUID]:
        """Return the plugin IDs selected for a workspace."""
        return self.workspace_activations.enabled_plugin_ids(workspace_id)

    def list_workspace_plugin_ids_many(
        self, workspace_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """Return ordered stored selections for already-authorized workspaces."""
        return self.workspace_activations.list_plugin_ids_many(workspace_ids)

    # -- Permissions ----------------------------------------------------------

    @staticmethod
    def can_edit(plugin: Plugin, *, user_id: int | None, admin: bool) -> bool:
        """Owners edit personal plugins, admins organization plugins."""
        if plugin.owner_id is not None:
            return user_id is not None and plugin.owner_id == user_id
        return plugin.organization_id is not None and admin

    def _editable_model(
        self, plugin_id: uuid.UUID, *, org_id: uuid.UUID, user: Any, admin: bool
    ) -> Plugin:
        plugin = self.get_visible_model(plugin_id, org_id=org_id, user=user)
        if (
            not admin
            and plugin.owner_id is None
            and not (
                plugin.enabled
                and plugin.published
                and plugin.id
                in self.org_activations.enabled_plugin_ids(
                    org_id, plugin_ids=[plugin.id]
                )
            )
        ):
            # Members never learn about shared plugins that are not released.
            raise NotFoundError("Plugin", str(plugin_id))
        if plugin.owner_id is None and plugin.organization_id is None:
            raise PluginPermissionError(
                "Built-in plugins cannot be changed; create a personal or "
                "organization plugin instead"
            )
        if not self.can_edit(plugin, user_id=getattr(user, "id", None), admin=admin):
            raise PluginPermissionError(
                "Only organization admins can change this plugin"
            )
        return plugin

    # -- Create / update / delete -------------------------------------------

    def replace_workspace_selection(
        self,
        workspace_id: uuid.UUID,
        plugin_ids: list[uuid.UUID],
        *,
        enabled_by: Any,
    ) -> None:
        """Persist an already-validated selection, deduplicating requested IDs.

        The workspace configuration service validates the final credential/plugin
        selection and holds its transaction and runner/workspace locks through
        this call, so both contexts' writes commit or roll back together.
        """
        self.workspace_activations.replace_for_workspace(
            workspace_id, list(dict.fromkeys(plugin_ids)), enabled_by=enabled_by
        )

    @transaction.atomic
    def create_plugin(
        self,
        *,
        org_id: uuid.UUID,
        user: Any,
        name: str,
        slug: str = "",
        scope: str = SCOPE_PERSONAL,
        admin: bool = False,
        description: str = "",
        enabled: bool = True,
        published: bool = True,
        skills: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        credential_requirements: list[dict] | None = None,
    ) -> dict:
        """Create a personal or (admins only) organization plugin, atomically."""
        if scope not in {SCOPE_PERSONAL, SCOPE_ORGANIZATION}:
            raise ValueError("Plugin scope must be 'personal' or 'organization'")
        if scope == SCOPE_ORGANIZATION and not admin:
            raise PluginPermissionError(
                "Only organization admins can create organization plugins"
            )
        name = validate_name(name, field="Plugin name")
        normalized_slug = normalize_slug(slug or name, field="Plugin slug")
        taken = (
            self.plugins.get_personal_by_slug(normalized_slug, user.id)
            if scope == SCOPE_PERSONAL
            else self.plugins.get_org_by_slug(normalized_slug, org_id)
        )
        if taken:
            raise ConflictError(f"A plugin named '{name}' already exists")
        description = (description or "").strip()
        if len(description) > 10_000:
            raise ValueError("Plugin description is too long")

        normalized_skills = [
            normalize_skill_payload(dict(s), i) for i, s in enumerate(skills or [])
        ]
        self._assert_unique_slugs([s["slug"] for s in normalized_skills], "Skill")
        normalized_mcps = [normalize_mcp_payload(dict(m)) for m in (mcp_servers or [])]
        self._assert_unique_slugs([m["slug"] for m in normalized_mcps], "MCP server")
        self._validate_normalized_oauth_urls(normalized_mcps)

        # Validate + prepare credential requirements before writing anything.
        prepared_requirements = self._prepare_requirement_inputs(
            credential_requirements or [],
            org_id=org_id,
            user=user,
            scope=scope,
        )
        self._validate_oauth_requirements(
            normalized_mcps,
            prepared_requirements,
            organization_id=org_id,
            plugin_slug=normalized_slug,
        )
        self._validate_no_oauth_placeholders(normalized_mcps, prepared_requirements)

        plugin = self.plugins.create(
            name=name,
            slug=normalized_slug,
            description=description,
            organization_id=org_id if scope == SCOPE_ORGANIZATION else None,
            owner=user if scope == SCOPE_PERSONAL else None,
            created_by=user,
            enabled=bool(enabled),
            published=bool(published),
        )
        self.skills.replace_for_plugin(plugin, self._without_ids(normalized_skills))
        self.mcp_servers.replace_for_plugin(
            plugin, self._without_ids(normalized_mcps)
        )
        self._store_values(
            plugin, prepared_requirements, org_id=org_id, user=user, admin=admin
        )
        self._create_requirements(plugin, prepared_requirements)
        logger.info("plugin_created", plugin_id=str(plugin.id), scope=scope)
        return self._serialize_plugins(
            [self.plugins.get_by_id(plugin.id)],
            org_id=org_id,
            user_id=user.id,
            admin=admin,
        )[0]

    @transaction.atomic
    def update_plugin(
        self,
        plugin_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        user: Any,
        admin: bool = False,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        published: bool | None = None,
        skills: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        credential_requirements: list[dict] | None = None,
    ) -> dict:
        """Replace plugin metadata and full component lists, atomically.

        Owners change personal plugins, admins organization plugins;
        built-in plugins are read-only (403). Component lists that are sent
        replace the stored lists; omitted lists are kept.
        """
        plugin = self._editable_model(plugin_id, org_id=org_id, user=user, admin=admin)

        fields: dict = {}
        if name is not None:
            fields["name"] = validate_name(name, field="Plugin name")
        if slug is not None:
            # An empty slug means "derive from the (new) name": clients never
            # send user-editable slugs and ship `slug: ''` so renames
            # re-generate the slug. `None` (field omitted) keeps the stored
            # slug untouched for API clients.
            slug_source = slug.strip() or fields.get("name") or plugin.name
            normalized = normalize_slug(slug_source, field="Plugin slug")
            existing = (
                self.plugins.get_personal_by_slug(normalized, plugin.owner_id)
                if plugin.owner_id is not None
                else self.plugins.get_org_by_slug(normalized, plugin.organization_id)
            )
            if existing is not None and existing.id != plugin.id:
                raise ConflictError(
                    f"A plugin named '{fields.get('name', normalized)}' already exists"
                )
            fields["slug"] = normalized
        if description is not None:
            description = (description or "").strip()
            if len(description) > 10_000:
                raise ValueError("Plugin description is too long")
            fields["description"] = description
        if enabled is not None:
            fields["enabled"] = bool(enabled)
        if published is not None:
            fields["published"] = bool(published)
        if fields:
            self.plugins.update(plugin, **fields)
            plugin = self.plugins.get_by_id(plugin.id)

        # Validate replacement lists before mutating stored components.
        normalized_skills = normalized_mcps = None
        if skills is not None:
            normalized_skills = [
                normalize_skill_payload(dict(s), i) for i, s in enumerate(skills)
            ]
            self._assert_unique_slugs([s["slug"] for s in normalized_skills], "Skill")
        stored_servers = self.mcp_servers.list_existing(plugin.id)
        if mcp_servers is not None:
            normalized_mcps = [normalize_mcp_payload(dict(m)) for m in mcp_servers]
            self._assert_unique_slugs(
                [m["slug"] for m in normalized_mcps], "MCP server"
            )
            self._validate_normalized_oauth_urls(normalized_mcps)
        prepared_requirements = None
        effective_plugin_slug = fields.get("slug", plugin.slug)
        if credential_requirements is not None:
            prepared_requirements = self._prepare_requirement_inputs(
                credential_requirements,
                org_id=org_id,
                user=user,
                scope=plugin.scope,
            )
        effective_mcps = (
            normalized_mcps
            if normalized_mcps is not None
            else [
                normalize_mcp_payload(
                    {
                        "name": m.name,
                        "slug": m.slug,
                        "transport": m.transport,
                        "command": m.command,
                        "args": m.args,
                        "cwd": m.cwd,
                        "env": m.env,
                        "resources": m.resources,
                        "tool_policy": m.tool_policy,
                        "runtime_artifact": m.runtime_artifact or None,
                        "url": m.url,
                        "headers": m.headers,
                        "auth_type": m.auth_type,
                        "oauth_requirement_key": m.oauth_requirement_key,
                        "startup_timeout_seconds": m.startup_timeout_seconds,
                        "request_timeout_seconds": m.request_timeout_seconds,
                    }
                )
                for m in stored_servers
            ]
        )
        self._validate_normalized_oauth_urls(effective_mcps)
        effective_reqs = (
            prepared_requirements
            if prepared_requirements is not None
            else [
                {"key": r.key, "service": r.credential_service}
                for r in self.requirements.list_for_plugin(plugin.id)
            ]
        )
        self._validate_oauth_requirements(
            effective_mcps,
            effective_reqs,
            organization_id=org_id,
            plugin_slug=effective_plugin_slug,
        )
        self._validate_no_oauth_placeholders(effective_mcps, effective_reqs)

        if normalized_skills is not None:
            self.skills.replace_for_plugin(plugin, normalized_skills)
        if normalized_mcps is not None:
            self.mcp_servers.replace_for_plugin(
                plugin, normalized_mcps
            )
        if prepared_requirements is not None:
            self._store_values(
                plugin, prepared_requirements, org_id=org_id, user=user, admin=admin
            )
            self._replace_requirements(plugin, prepared_requirements)
        # Touch the plugin so running agents notice changed components.
        self.plugins.update(plugin)

        logger.info("plugin_updated", plugin_id=str(plugin.id))
        return self._serialize_plugins(
            [self.plugins.get_by_id(plugin.id)],
            org_id=org_id,
            user_id=getattr(user, "id", None),
            admin=admin,
        )[0]

    @transaction.atomic
    def delete_plugin(
        self, plugin_id: uuid.UUID, *, org_id: uuid.UUID, user: Any, admin: bool
    ) -> Plugin:
        """Delete a personal or organization plugin; credentials are kept."""
        plugin = self._editable_model(plugin_id, org_id=org_id, user=user, admin=admin)
        self.requirements.delete_for_plugin(plugin.id)
        self.plugins.delete(plugin.id)
        logger.info("plugin_deleted", plugin_id=str(plugin_id))
        return plugin

    # -- Org activation ------------------------------------------------------

    def set_org_activation(
        self,
        plugin_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        user,
        active: bool,
    ) -> dict:
        """Release or withdraw a built-in/organization plugin org-wide.

        Workspace selections are kept. Activation requires an enabled and
        published definition (409 ``plugin_not_available`` otherwise);
        deactivation always succeeds. Personal plugins need no release.
        """
        plugin = self.get_visible_model(plugin_id, org_id=org_id, user=user)
        if plugin.owner_id is not None:
            raise ConflictError(
                "Personal plugins are always available to their owner and are "
                "not released for the organization",
                code="personal_plugin",
            )
        if active and not (plugin.enabled and plugin.published):
            raise ConflictError(
                f"Plugin '{plugin.slug}' is not available for activation "
                "(it is disabled or unpublished)",
                code="plugin_not_available",
            )
        self.org_activations.set_enabled(
            org_id=org_id, plugin=plugin, enabled_by=user, active=bool(active)
        )
        return self._serialize_plugins(
            [self.plugins.get_by_id(plugin.id)],
            org_id=org_id,
            user_id=getattr(user, "id", None),
            admin=True,
        )[0]

    # -- Workspace activation -------------------------------------------------

    def _workspace_required_gaps(
        self,
        *,
        plugins: list[Plugin],
        credentials: list,
        owner_id: int,
        org_id: uuid.UUID,
    ) -> dict[uuid.UUID, list[dict]]:
        """Batch dependency reads and apply fail-closed ownership checks once."""
        from apps.credentials.mcp_oauth import oauth_credential_status

        if not plugins:
            return {}
        plugins_by_id = {plugin.id: plugin for plugin in plugins}
        requirements = list(self.requirements.list_for_plugins(list(plugins_by_id)))
        oauth_plugin_ids = list(
            {
                requirement.plugin_id
                for requirement in requirements
                if requirement.required
                and requirement.credential_service.credential_type == "mcp_oauth"
            }
        )
        servers_by_requirement: dict[tuple[uuid.UUID, str], list[PluginMcpServer]] = (
            defaultdict(list)
        )
        for server in self.mcp_servers.list_oauth_for_plugins(oauth_plugin_ids):
            servers_by_requirement[
                (server.plugin_id, server.oauth_requirement_key)
            ].append(server)

        valid_by_service: dict[uuid.UUID, list] = {}
        for credential in credentials:
            if usable_credential(
                credential,
                service_id=credential.service_id,
                owner_id=owner_id,
                org_id=org_id,
            ):
                valid_by_service.setdefault(credential.service_id, []).append(
                    credential
                )

        gaps: dict[uuid.UUID, list[dict]] = {
            plugin_id: [] for plugin_id in plugins_by_id
        }
        for requirement in requirements:
            if not requirement.required:
                continue
            # The plugin's own credential wins over one attached to the workspace.
            linked = requirement.credential
            candidates = (
                [linked]
                if usable_credential(
                    linked,
                    service_id=requirement.credential_service_id,
                    owner_id=owner_id,
                    org_id=org_id,
                )
                else valid_by_service.get(requirement.credential_service_id, [])
            )
            ready = bool(candidates)
            if requirement.credential_service.credential_type == "mcp_oauth":
                servers = servers_by_requirement.get(
                    (requirement.plugin_id, requirement.key), []
                )
                ready = bool(servers) and all(
                    requirement.credential_service.oauth_server_url == server.url
                    and any(
                        candidate.oauth_server_url == server.url
                        and oauth_credential_status(candidate)["connected"]
                        for candidate in candidates
                    )
                    for server in servers
                )
            if not ready:
                plugin = plugins_by_id[requirement.plugin_id]
                gaps[plugin.id].append(
                    {
                        "plugin_id": str(plugin.id),
                        "plugin_name": plugin.name,
                        "key": requirement.key,
                        "service_id": str(requirement.credential_service_id),
                        "service_name": requirement.credential_service.name,
                        "credential_type": (
                            requirement.credential_service.credential_type
                        ),
                    }
                )
        return gaps

    def _available_for_owner(
        self,
        *,
        org_id: uuid.UUID,
        owner_id: int | None,
        visible: list[Plugin] | None = None,
    ) -> list[Plugin]:
        """Plugins a workspace of *owner_id* may enable, ordered by name.

        *visible* reuses an already loaded ``list_visible`` result.
        """
        released = self.org_activations.enabled_plugin_ids(org_id)
        if visible is None:
            visible = list(self.plugins.list_visible(org_id, owner_id))
        return [
            plugin
            for plugin in visible
            if plugin.owner_id is not None
            or (plugin.id in released and plugin.enabled and plugin.published)
        ]

    def validate_workspace_plugin_selection(
        self,
        *,
        plugin_ids: list[uuid.UUID],
        credentials: list,
        owner,
        org_id: uuid.UUID,
        allow_unavailable: bool = False,
    ) -> list[dict]:
        """Validate a complete plugin selection and return missing requirements."""
        desired = list(dict.fromkeys(plugin_ids))
        owner_id = getattr(owner, "id", None)
        loaded = list(self.plugins.list_visible(org_id, owner_id))
        visible = {p.id: p for p in loaded}
        unknown = [pid for pid in desired if pid not in visible]
        if unknown:
            raise NotFoundError("Plugin", ", ".join(str(pid) for pid in unknown))
        available = {
            p.id
            for p in self._available_for_owner(
                org_id=org_id, owner_id=owner_id, visible=loaded
            )
        }
        unavailable = [pid for pid in desired if pid not in available]
        if unavailable and not allow_unavailable:
            raise ConflictError(
                "Plugins are not available for this organization: "
                + ", ".join(str(pid) for pid in unavailable),
                code="plugin_not_available",
            )
        plugins = [visible[pid] for pid in desired if pid not in unavailable]
        gaps = self._workspace_required_gaps(
            plugins=plugins,
            credentials=credentials,
            owner_id=owner_id,
            org_id=org_id,
        )
        return [gap for plugin in plugins for gap in gaps[plugin.id]]

    def list_workspace_plugins(self, *, workspace, org_id: uuid.UUID) -> list[dict]:
        """List the plugins a workspace may enable, with state + credential gaps."""
        plugins = self._available_for_owner(
            org_id=org_id, owner_id=workspace.created_by_id
        )
        ws_enabled = self.workspace_activations.enabled_plugin_ids(workspace.id)
        attached_credentials = CredentialSvc().list_workspace_credentials(workspace.id)
        gaps = self._workspace_required_gaps(
            plugins=plugins,
            credentials=attached_credentials,
            owner_id=workspace.created_by_id,
            org_id=org_id,
        )
        result = []
        for plugin in plugins:
            missing = gaps[plugin.id]
            result.append(
                {
                    "id": plugin.id,
                    "name": plugin.name,
                    "slug": plugin.slug,
                    "description": plugin.description,
                    "organization_id": plugin.organization_id,
                    "is_global": plugin.scope == SCOPE_BUILTIN,
                    "scope": plugin.scope,
                    "workspace_enabled": plugin.id in ws_enabled,
                    "missing_required_credentials": missing,
                    "ready": not missing,
                }
            )
        return result

    # -- Credential removal guard --------------------------------------------

    def credential_removal_gaps(
        self, credential, *, workspace_id: uuid.UUID, org_id: uuid.UUID, owner_id=None
    ) -> list[dict]:
        """Return a workspace's effective plugin requirements *credential* backs.

        ``org_id`` is the workspace's organization and ``owner_id`` its
        owner; the caller enumerates the workspaces that have *credential*
        attached.
        """
        selected = self.workspace_activations.enabled_plugin_ids(workspace_id)
        gaps = []
        for plugin in self._available_for_owner(org_id=org_id, owner_id=owner_id):
            if plugin.id not in selected:
                continue
            for requirement in self.requirements.list_for_plugin(plugin.id):
                if usable_credential(
                    requirement.credential,
                    service_id=requirement.credential_service_id,
                    owner_id=owner_id,
                    org_id=org_id,
                ) and requirement.credential_id != credential.id:
                    # The plugin brings its own credential; the attachment
                    # is not what it runs on.
                    continue
                if (
                    requirement.required
                    and requirement.credential_service_id == credential.service_id
                ):
                    gaps.append(
                        {
                            "workspace_id": str(workspace_id),
                            "plugin_id": str(plugin.id),
                            "plugin_name": plugin.name,
                            "key": requirement.key,
                            "service_id": str(requirement.credential_service_id),
                            "service_name": requirement.credential_service.name,
                        }
                    )
        return gaps

    # -- Internal helpers ----------------------------------------------------

    @staticmethod
    def _without_ids(items: list[dict]) -> list[dict]:
        """New plugins never adopt component IDs a client sent."""
        return [{**item, "id": None} for item in items]

    def _serialize_plugins(
        self,
        plugins: list[Plugin],
        *,
        org_id: uuid.UUID,
        user_id: int | None = None,
        admin: bool = True,
    ) -> list[dict]:
        """Load components once for the requested definitions, preserving order."""
        if not plugins:
            return []
        plugin_ids = [plugin.id for plugin in plugins]
        skills: dict[uuid.UUID, list[PluginSkill]] = defaultdict(list)
        mcps: dict[uuid.UUID, list[PluginMcpServer]] = defaultdict(list)
        reqs: dict[uuid.UUID, list[PluginCredentialRequirement]] = defaultdict(list)
        for skill in self.skills.list_for_plugins(plugin_ids):
            skills[skill.plugin_id].append(skill)
        for server in self.mcp_servers.list_for_plugins(plugin_ids):
            mcps[server.plugin_id].append(server)
        for requirement in self.requirements.list_for_plugins(plugin_ids):
            reqs[requirement.plugin_id].append(requirement)
        enabled_ids = self.org_activations.enabled_plugin_ids(
            org_id, plugin_ids=plugin_ids
        )
        return [
            self._serialize_plugin(
                plugin,
                skills=skills[plugin.id],
                mcps=mcps[plugin.id],
                reqs=reqs[plugin.id],
                org_enabled=plugin.id in enabled_ids,
                editable=self.can_edit(plugin, user_id=user_id, admin=admin),
                org_id=org_id,
            )
            for plugin in plugins
        ]

    @staticmethod
    def _credential_view(requirement, org_id: uuid.UUID) -> dict:
        """The plugin's own credential of a requirement, if usable here."""
        credential = requirement.credential
        if credential is None or credential.organization_id not in {None, org_id}:
            return {
                "credential_id": None,
                "credential_name": "",
                "credential_scope": "",
            }
        return {
            "credential_id": credential.id,
            "credential_name": credential.name,
            "credential_scope": (
                "personal" if credential.user_id is not None else "organization"
            ),
        }

    @staticmethod
    def _serialize_plugin(
        plugin: Plugin,
        *,
        skills: list[PluginSkill],
        mcps: list[PluginMcpServer],
        reqs: list[PluginCredentialRequirement],
        org_enabled: bool,
        editable: bool = False,
        org_id: uuid.UUID | None = None,
    ) -> dict:
        """Serialize a fully loaded definition without further repository reads."""
        scope = plugin.scope
        return {
            "id": plugin.id,
            "name": plugin.name,
            "slug": plugin.slug,
            "description": plugin.description,
            "scope": scope,
            "enabled": plugin.enabled,
            "published": plugin.published,
            "organization_id": plugin.organization_id,
            "is_global": scope == SCOPE_BUILTIN,
            "org_enabled": org_enabled,
            "available": scope == SCOPE_PERSONAL
            or (org_enabled and plugin.enabled and plugin.published),
            "editable": editable,
            "skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "slug": s.slug,
                    "description": s.description,
                    "body": s.body,
                    "position": s.position,
                }
                for s in skills
            ],
            "mcp_servers": [
                {
                    "id": m.id,
                    "name": m.name,
                    "slug": m.slug,
                    "transport": m.transport,
                    "command": m.command,
                    "args": list(m.args or []),
                    "cwd": m.cwd,
                    "env": dict(m.env or {}),
                    "resources": dict(m.resources or {}),
                    "tool_policy": dict(m.tool_policy or {}),
                    "runtime_artifact": dict(m.runtime_artifact)
                    if m.runtime_artifact
                    else None,
                    "url": m.url,
                    "headers": dict(m.headers or {}),
                    "auth_type": m.auth_type,
                    "oauth_requirement_key": m.oauth_requirement_key,
                }
                for m in mcps
            ],
            "credential_requirements": [
                {
                    "id": r.id,
                    "key": r.key,
                    "description": r.description,
                    "required": r.required,
                    "service_id": r.credential_service_id,
                    "service_name": r.credential_service.name,
                    "service_slug": r.credential_service.slug,
                    "credential_type": r.credential_service.credential_type,
                    **PluginService._credential_view(r, org_id),
                }
                for r in reqs
            ],
            "created_at": plugin.created_at,
            "updated_at": plugin.updated_at,
        }

    @staticmethod
    def _validate_normalized_oauth_urls(servers: list[dict]) -> None:
        """Reject non-HTTPS legacy OAuth server URLs before runtime use."""
        for server in servers:
            if server.get("auth_type", "none") == "oauth":
                validate_oauth_server_url(server.get("url", ""))

    @staticmethod
    def _validate_no_oauth_placeholders(
        servers: list[dict], requirements: list[dict]
    ) -> None:
        oauth_keys = {
            item["key"]
            for item in requirements
            if item["service"].credential_type == "mcp_oauth"
        }
        for server in servers:
            for mapping in (server.get("env") or {}, server.get("headers") or {}):
                for raw_value in mapping.values():
                    if oauth_keys.intersection(_PLACEHOLDER_RE.findall(raw_value)):
                        raise ValueError(
                            "MCP OAuth credentials cannot be used as "
                            "plugin placeholders"
                        )

    @staticmethod
    def _validate_oauth_requirements(
        servers: list[dict],
        requirements: list[dict],
        *,
        organization_id,
        plugin_slug: str,
    ) -> None:
        by_key = {item["key"]: item for item in requirements}
        oauth_keys = set()
        for server in servers:
            if server.get("auth_type", "none") != "oauth":
                continue
            if server.get("transport") == "stdio":
                raise ValueError("OAuth is supported only on HTTP MCP transports")
            key = server.get("oauth_requirement_key", "")
            item = by_key.get(key)
            service = item.get("service") if item else None
            if (
                service is None
                or service.credential_type != "mcp_oauth"
                or not item.get("required", True)
            ):
                raise ValueError(
                    f"OAuth server requirement '{key}' must reference a "
                    "required MCP OAuth service"
                )
            endpoint = validate_oauth_server_url(server.get("url", ""))
            if service.oauth_server_url != endpoint:
                raise ValueError(
                    "OAuth server URL must exactly match the credential "
                    "service endpoint"
                )
            if service.organization_id not in {None, organization_id}:
                raise ValueError(
                    "Credential service is not visible to the plugin organization"
                )
            oauth_keys.add(key)
        for item in requirements:
            if (
                item["service"].credential_type == "mcp_oauth"
                and item["key"] not in oauth_keys
            ):
                raise ValueError(
                    "MCP OAuth services may only be used by OAuth MCP servers"
                )

    @staticmethod
    def _assert_unique_slugs(slugs: list[str], label: str) -> None:
        seen: set[str] = set()
        for slug in slugs:
            if slug in seen:
                raise ConflictError(f"Duplicate {label} slug '{slug}'")
            seen.add(slug)

    def _prepare_requirement_inputs(
        self,
        raw_requirements: list[dict],
        *,
        org_id: uuid.UUID,
        user: Any = None,
        scope: str = SCOPE_ORGANIZATION,
    ) -> list[dict]:
        """Validate the credential links of a plugin before anything is written.

        Each entry names a visible credential service (``service_id``) or a
        new secret service to create (``new_service``: name, env var), and
        optionally the credential the plugin always uses (``credential_id``)
        and/or a ``value`` to store as that credential. New services are
        created here (inside the caller's transaction) so the remaining
        validation sees real services; values are stored on persist.
        """
        credential_svc = CredentialSvc()
        prepared, seen_keys, seen_services = [], set(), set()
        for raw in raw_requirements:
            item = dict(raw) if isinstance(raw, dict) else {}
            key = normalize_requirement_key(item.get("key", ""))
            if key in seen_keys:
                raise ConflictError(f"Duplicate credential requirement key '{key}'")
            seen_keys.add(key)
            description = (item.get("description") or "").strip()
            if len(description) > 2000:
                raise ValueError("Credential requirement description is too long")
            value = item.get("value")
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"Credential '{key}' needs a non-empty value")
            if isinstance(value, str) and len(value) > 64 * 1024:
                raise ValueError(f"Credential '{key}' value is too long")
            new_service = item.get("new_service")
            if new_service:
                service = CredentialServiceSvc().create_secret_service(
                    name=str(new_service.get("name") or key),
                    env_var_name=str(new_service.get("env_var_name") or key),
                    description=description,
                    owner=user if scope == SCOPE_PERSONAL else None,
                    organization_id=None if scope == SCOPE_PERSONAL else org_id,
                )
            else:
                try:
                    service_id = uuid.UUID(str(item.get("service_id") or ""))
                except ValueError:
                    raise ValueError(
                        f"Credential '{key}' needs service_id or new_service"
                    ) from None
                service = CredentialServiceSvc().get_service(
                    service_id, org_id=org_id, user=user
                )
            if scope == SCOPE_ORGANIZATION and service.owner_id is not None:
                raise ValueError(
                    "Organization plugins cannot use personal credential services"
                )
            if service.id in seen_services:
                raise ConflictError("Duplicate credential service in requirements")
            seen_services.add(service.id)
            credential = None
            if item.get("credential_id"):
                try:
                    credential_id = uuid.UUID(str(item["credential_id"]))
                except ValueError:
                    raise ValueError("credential_id must be a UUID") from None
                credential = credential_svc.get_visible_credential(
                    credential_id, user=user, org_id=org_id
                )
                if credential.service_id != service.id:
                    raise ValueError(
                        f"Credential '{credential.name}' does not belong to the "
                        f"credential service '{service.name}'"
                    )
                self._assert_credential_scope(credential, scope=scope, org_id=org_id)
            if value is not None and service.credential_type in {
                "mcp_oauth",
                "ssh_key",
            }:
                raise ValueError(
                    f"Credential '{key}' of type {service.credential_type} cannot "
                    "take a value here; connect or create it under Credentials"
                )
            prepared.append(
                {
                    "key": key,
                    "description": description,
                    "required": bool(item.get("required", True)),
                    "service": service,
                    "credential": credential,
                    "value": value,
                }
            )
        return prepared

    @staticmethod
    def _assert_credential_scope(credential, *, scope: str, org_id: uuid.UUID) -> None:
        """Organization plugins share organization credentials only."""
        if scope == SCOPE_ORGANIZATION and credential.organization_id != org_id:
            raise ValueError(
                "Organization plugins can only use organization credentials; "
                "personal credentials stay with their owner"
            )

    def _store_values(
        self,
        plugin: Plugin,
        prepared: list[dict],
        *,
        org_id: uuid.UUID,
        user: Any,
        admin: bool,
    ) -> None:
        """Store entered values as credentials and link them to the plugin.

        A value updates the linked credential when the caller may edit it,
        otherwise it becomes a new credential: personal for personal
        plugins, an organization credential for organization plugins.
        """
        credential_svc = CredentialSvc()
        personal = plugin.owner_id is not None
        for item in prepared:
            value = item.get("value")
            if value is None:
                continue
            linked = item.get("credential")
            if linked is not None and credential_svc.can_edit(
                linked, user=user, org_id=org_id, is_admin=admin
            ):
                item["credential"] = credential_svc.update_credential(
                    credential_id=linked.id,
                    org_id=org_id,
                    user=user,
                    is_admin=admin,
                    value=value,
                )
                continue
            label = item["service"].env_var_name or item["key"]
            name = f"{plugin.name}: {label}"[:255]
            item["credential"] = (
                credential_svc.create_personal_credential(
                    service_id=item["service"].id,
                    name=name,
                    value=value,
                    user=user,
                    org_id=org_id,
                )
                if personal
                else credential_svc.create_org_credential(
                    organization_id=org_id,
                    service_id=item["service"].id,
                    name=name,
                    value=value,
                    user=user,
                )
            )

    def _create_requirements(self, plugin, prepared: list[dict]) -> None:
        """Persist the plugin's credential links."""
        self.requirements.bulk_create_for_plugin(plugin, prepared)

    def _replace_requirements(self, plugin, prepared: list[dict]) -> None:
        """Replace the plugin's credential links (services stay)."""
        self.requirements.delete_for_plugin(plugin.id)
        self._create_requirements(plugin, prepared)
