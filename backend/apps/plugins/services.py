"""
Service layer for the plugins app.

All business logic lives here. The API layer delegates to these methods;
repositories encapsulate all ORM access.
"""

from __future__ import annotations

import re
import uuid

import structlog
from django.db import transaction
from django.utils.text import slugify

from common.exceptions import ConflictError, NotFoundError

from .models import Plugin, PluginTransport
from .repositories import (
    OrgPluginActivationRepository,
    PluginCredentialRequirementRepository,
    PluginMcpServerRepository,
    PluginRepository,
    PluginSkillRepository,
    WorkspacePluginActivationRepository,
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
#: DISPLAY is allowed through the stream env sanitizer (not in the
#: blocked HOME/PATH/LD_… set); XAUTHORITY pins the runner-owned file
#: so X11 clients resolve auth instead of depending on ambient state.
PLAYWRIGHT_MCP_ENV = {
    "DISPLAY": ":1",
    "XAUTHORITY": "/root/.Xauthority",
}


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
    """Validate a nested skill payload."""
    name = validate_name(item.get("name", ""), field="Skill name")
    slug = normalize_slug(item.get("slug") or item.get("name", ""), field="Skill slug")
    body = item.get("body") or ""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Skill body must not be empty")
    if len(body) > _MAX_BODY_LEN:
        raise ValueError(f"Skill body must be at most {_MAX_BODY_LEN} characters")
    try:
        position = int(item.get("position", index))
    except (TypeError, ValueError):
        raise ValueError("Skill position must be an integer") from None
    if position < 0 or position > 10_000:
        raise ValueError("Skill position must be between 0 and 10000")
    return {"name": name, "slug": slug, "body": body, "position": position}


def validate_oauth_server_url(value: str) -> str:
    """Plugin-level alias to credential-owned OAuth endpoint validation."""
    from apps.credentials.services import CredentialServiceSvc

    return CredentialServiceSvc._validate_oauth_server_url(value)


def normalize_mcp_payload(item: dict) -> dict:
    """Validate a nested MCP server payload."""
    name = validate_name(item.get("name", ""), field="MCP server name")
    slug = normalize_slug(
        item.get("slug") or item.get("name", ""), field="MCP server slug"
    )
    transport = (item.get("transport") or "stdio").strip()
    valid_transports = {c.value for c in PluginTransport}
    if transport not in valid_transports:
        raise ValueError(f"MCP transport must be one of {sorted(valid_transports)}")

    command = item.get("command") or ""
    if not isinstance(command, str):
        raise ValueError("MCP command must be a string")
    command = command.strip()
    args = item.get("args") or []
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("MCP args must be a list of strings")
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
    env = validate_placeholder_mapping(item.get("env") or {}, field="MCP env")
    headers = validate_placeholder_mapping(
        item.get("headers") or {}, field="MCP headers"
    )

    try:
        startup_timeout = int(item.get("startup_timeout_seconds", 30))
        request_timeout = int(item.get("request_timeout_seconds", 60))
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
        "name": name,
        "slug": slug,
        "transport": transport,
        "command": command,
        "args": list(args),
        "cwd": cwd,
        "env": env,
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


class PluginService:
    """Business logic for plugins and their activations."""

    def __init__(self) -> None:
        self.plugins = PluginRepository
        self.skills = PluginSkillRepository
        self.mcp_servers = PluginMcpServerRepository
        self.requirements = PluginCredentialRequirementRepository
        self.org_activations = OrgPluginActivationRepository
        self.workspace_activations = WorkspacePluginActivationRepository

    # -- Queries ------------------------------------------------------------

    def list_visible(self, *, org_id: uuid.UUID) -> list[dict]:
        """Return visible plugin definitions and their service dependencies."""
        plugins = list(self.plugins.list_visible_to_org(org_id))
        return [self._serialize(p, org_id=org_id) for p in plugins]

    def get_visible(self, plugin_id: uuid.UUID, *, org_id: uuid.UUID) -> dict:
        """Return one visible plugin or raise NotFoundError (cross-org 404)."""
        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None:
            raise NotFoundError("Plugin", str(plugin_id))
        return self._serialize(plugin, org_id=org_id)

    def get_visible_model(self, plugin_id: uuid.UUID, *, org_id: uuid.UUID):
        """Return the visible plugin model or raise NotFoundError."""
        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None:
            raise NotFoundError("Plugin", str(plugin_id))
        return plugin

    # -- Create / update / delete -------------------------------------------

    @transaction.atomic
    def create_org_plugin(
        self,
        *,
        org_id: uuid.UUID,
        user,
        name: str,
        slug: str,
        description: str = "",
        enabled: bool = True,
        published: bool = True,
        skills: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        credential_requirements: list[dict] | None = None,
    ):
        """Create an org-owned plugin with nested components, atomically."""
        from apps.credentials.repositories import CredentialServiceRepository

        name = validate_name(name, field="Plugin name")
        normalized_slug = normalize_slug(slug or name, field="Plugin slug")
        if self.plugins.get_org_by_slug(normalized_slug, org_id):
            raise ConflictError(
                f"Plugin slug '{normalized_slug}' already exists in this organization"
            )
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
            service_repo=CredentialServiceRepository,
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
            organization_id=org_id,
            created_by=user,
            enabled=bool(enabled),
            published=bool(published),
        )
        self.skills.replace_for_plugin(plugin, normalized_skills)
        self.mcp_servers.replace_for_plugin(plugin, normalized_mcps)
        self._create_requirements(plugin, prepared_requirements)
        logger.info("Plugin created: %s (org=%s)", plugin.id, org_id)
        return self._serialize(self.plugins.get_by_id(plugin.id), org_id=org_id)

    @transaction.atomic
    def update_org_plugin(
        self,
        plugin_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        user,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        published: bool | None = None,
        skills: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        credential_requirements: list[dict] | None = None,
    ) -> dict:
        """Replace org-owned plugin metadata + full component lists, atomically.

        Only org-owned plugins of the caller's org may be updated; global or
        foreign plugins raise NotFoundError (404).
        """
        from apps.credentials.repositories import CredentialServiceRepository

        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None or plugin.organization_id != org_id:
            raise NotFoundError("Plugin", str(plugin_id))

        fields: dict = {}
        if name is not None:
            fields["name"] = validate_name(name, field="Plugin name")
        if slug is not None:
            # An empty slug means "derive from the (new) name": the webapp
            # never sends user-editable slugs and always ships `slug: ''`
            # so renames re-generate the slug. `None` (field omitted) keeps
            # the stored slug untouched for API clients.
            slug_source = slug.strip() or fields.get("name") or plugin.name
            normalized = normalize_slug(slug_source, field="Plugin slug")
            existing = self.plugins.get_org_by_slug(normalized, org_id)
            if existing is not None and existing.id != plugin.id:
                raise ConflictError(
                    f"Plugin slug '{normalized}' already exists in this organization"
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
                service_repo=CredentialServiceRepository,
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
                        "url": m.url,
                        "headers": m.headers,
                        "auth_type": m.auth_type,
                        "oauth_requirement_key": m.oauth_requirement_key,
                        "startup_timeout_seconds": m.startup_timeout_seconds,
                        "request_timeout_seconds": m.request_timeout_seconds,
                    }
                )
                for m in self.mcp_servers.list_for_plugin(plugin.id)
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
            self.mcp_servers.replace_for_plugin(plugin, normalized_mcps)
        if prepared_requirements is not None:
            self._replace_requirements(plugin, prepared_requirements)

        logger.info("Plugin updated: %s (org=%s)", plugin.id, org_id)
        return self._serialize(self.plugins.get_by_id(plugin.id), org_id=org_id)

    @transaction.atomic
    def delete_org_plugin(self, plugin_id: uuid.UUID, *, org_id: uuid.UUID) -> None:
        """Delete an org-owned plugin without deleting its services or credentials."""
        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None or plugin.organization_id != org_id:
            raise NotFoundError("Plugin", str(plugin_id))
        self.requirements.delete_for_plugin(plugin.id)
        self.plugins.delete(plugin.id)
        logger.info("Plugin deleted: %s (org=%s)", plugin_id, org_id)

    # -- Org activation ------------------------------------------------------

    def set_org_activation(
        self,
        plugin_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        user,
        active: bool,
    ) -> dict:
        """Enable/disable a visible plugin org-wide (workspace rows kept).

        Activation (``active=True``) requires an *effective* definition:
        the plugin must be ``enabled`` and ``published``. Disabled or
        unpublished plugins (global or org-owned) cannot be released —
        a 409 ``plugin_not_available`` is raised. Deactivation
        (``active=False``) always succeeds (idempotent).
        """
        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None:
            raise NotFoundError("Plugin", str(plugin_id))
        if active and not (plugin.enabled and plugin.published):
            raise ConflictError(
                f"Plugin '{plugin.slug}' is not available for activation "
                "(it is disabled or unpublished)",
                code="plugin_not_available",
            )
        self.org_activations.set_enabled(
            org_id=org_id, plugin=plugin, enabled_by=user, active=bool(active)
        )
        return self._serialize(self.plugins.get_by_id(plugin.id), org_id=org_id)

    # -- Workspace activation -------------------------------------------------

    def _workspace_required_gaps(
        self,
        *,
        plugin: Plugin,
        credentials: list,
        owner_id: int,
        org_id: uuid.UUID,
    ) -> list[dict]:
        """Return required gaps after fail-closed credential ownership checks."""
        from apps.credentials.mcp_oauth import oauth_credential_status

        valid_by_service: dict[uuid.UUID, list] = {}
        for credential in credentials:
            service = credential.service
            if service.organization_id not in {None, org_id}:
                continue
            if credential.user_id is not None:
                if (
                    credential.user_id != owner_id
                    or credential.organization_id is not None
                ):
                    continue
            elif credential.organization_id != org_id:
                continue
            valid_by_service.setdefault(credential.service_id, []).append(credential)

        gaps = []
        requirements = self.requirements.list_for_plugin(plugin.id)
        for requirement in requirements:
            if not requirement.required:
                continue
            candidates = valid_by_service.get(requirement.credential_service_id, [])
            ready = bool(candidates)
            if requirement.credential_service.credential_type == "mcp_oauth":
                servers = self.mcp_servers.list_oauth_for_requirement(
                    plugin.id, requirement.key
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
                gaps.append(
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
        visible = {p.id: p for p in self.plugins.list_visible_to_org(org_id)}
        unknown = [pid for pid in desired if pid not in visible]
        if unknown:
            raise NotFoundError("Plugin", ", ".join(str(pid) for pid in unknown))
        enabled_ids = self.org_activations.enabled_plugin_ids(org_id)
        unavailable = [
            pid
            for pid in desired
            if pid not in enabled_ids
            or not visible[pid].enabled
            or not visible[pid].published
        ]
        if unavailable and not allow_unavailable:
            raise ConflictError(
                "Plugins are not available for this organization: "
                + ", ".join(str(pid) for pid in unavailable),
                code="plugin_not_available",
            )
        gaps = []
        for plugin_id in desired:
            if plugin_id not in unavailable:
                gaps.extend(
                    self._workspace_required_gaps(
                        plugin=visible[plugin_id],
                        credentials=credentials,
                        owner_id=owner.id,
                        org_id=org_id,
                    )
                )
        return gaps

    def list_workspace_plugins(self, *, workspace, org_id: uuid.UUID) -> list[dict]:
        """List org-enabled plugins with workspace state + credential gaps."""

        plugins = [
            p
            for p in self.plugins.list_visible_to_org(org_id)
            if p.enabled and p.published
        ]
        enabled_ids = self.org_activations.enabled_plugin_ids(org_id)
        plugins = [p for p in plugins if p.id in enabled_ids]
        ws_enabled = self.workspace_activations.enabled_plugin_ids(workspace.id)
        attached_credentials = self.plugins.list_workspace_credentials(workspace.id)
        result = []
        for plugin in plugins:
            missing = self._workspace_required_gaps(
                plugin=plugin,
                credentials=attached_credentials,
                owner_id=workspace.created_by_id,
                org_id=org_id,
            )
            result.append(
                {
                    "id": plugin.id,
                    "name": plugin.name,
                    "slug": plugin.slug,
                    "description": plugin.description,
                    "organization_id": plugin.organization_id,
                    "is_global": plugin.organization_id is None,
                    "workspace_enabled": plugin.id in ws_enabled,
                    "missing_required_credentials": missing,
                    "ready": not missing,
                }
            )
        return result

    # -- Credential deletion guard -------------------------------------------

    def blocking_plugin_gaps_for_credential(
        self, credential, *, org_id: uuid.UUID
    ) -> list[dict]:
        """Return required selected-plugin dependencies for a credential."""
        from apps.runners.repositories import WorkspaceRepository

        gaps = []
        for workspace_id in WorkspaceRepository.list_ids_for_credential(credential.id):
            workspace = WorkspaceRepository.get_by_id(workspace_id)
            if workspace is None or workspace.runner.organization_id is None:
                continue
            workspace_org_id = workspace.runner.organization_id
            selected = self.workspace_activations.enabled_plugin_ids(workspace.id)
            effective = selected & self.org_activations.enabled_plugin_ids(
                workspace_org_id
            )
            for plugin in self.plugins.list_visible_to_org(workspace_org_id):
                if plugin.id not in effective or not (
                    plugin.enabled and plugin.published
                ):
                    continue
                for requirement in self.requirements.list_for_plugin(plugin.id):
                    if (
                        requirement.required
                        and requirement.credential_service_id == credential.service_id
                    ):
                        gaps.append(
                            {
                                "workspace_id": str(workspace.id),
                                "plugin_id": str(plugin.id),
                                "plugin_name": plugin.name,
                                "key": requirement.key,
                                "service_id": str(requirement.credential_service_id),
                                "service_name": requirement.credential_service.name,
                            }
                        )
        return gaps

    # -- Internal helpers ----------------------------------------------------

    def _serialize(self, plugin, *, org_id: uuid.UUID) -> dict:
        """Serialize a plugin definition and dependency mapping."""
        skills = list(self.skills.list_for_plugin(plugin.id))
        mcps = list(self.mcp_servers.list_for_plugin(plugin.id))
        reqs = list(self.requirements.list_for_plugin(plugin.id))
        return {
            "id": plugin.id,
            "name": plugin.name,
            "slug": plugin.slug,
            "description": plugin.description,
            "enabled": plugin.enabled,
            "published": plugin.published,
            "organization_id": plugin.organization_id,
            "is_global": plugin.organization_id is None,
            "org_enabled": self.org_activations.is_enabled(org_id, plugin.id),
            "skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "slug": s.slug,
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
                    "url": m.url,
                    "headers": dict(m.headers or {}),
                    "auth_type": m.auth_type,
                    "oauth_requirement_key": m.oauth_requirement_key,
                    "startup_timeout_seconds": m.startup_timeout_seconds,
                    "request_timeout_seconds": m.request_timeout_seconds,
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
            from apps.credentials.services import CredentialServiceSvc

            endpoint = CredentialServiceSvc._validate_oauth_server_url(
                server.get("url", "")
            )
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
        service_repo,
    ) -> list[dict]:
        """Validate dependencies and resolve only existing visible services."""
        prepared, seen_keys, seen_services = [], set(), set()
        for raw in raw_requirements:
            item = dict(raw) if isinstance(raw, dict) else {}
            key = normalize_requirement_key(item.get("key", ""))
            if key in seen_keys:
                raise ConflictError(f"Duplicate credential requirement key '{key}'")
            seen_keys.add(key)
            service_id = item.get("service_id")
            try:
                service_id = uuid.UUID(str(service_id or ""))
            except ValueError:
                raise ValueError("service_id must be a UUID") from None
            service = service_repo.get_visible_by_id(service_id, org_id)
            if service is None:
                raise NotFoundError("CredentialService", str(service_id))
            if service.id in seen_services:
                raise ConflictError("Duplicate credential service in requirements")
            seen_services.add(service.id)
            description = (item.get("description") or "").strip()
            if len(description) > 2000:
                raise ValueError("Credential requirement description is too long")
            prepared.append(
                {
                    "key": key,
                    "description": description,
                    "required": bool(item.get("required", True)),
                    "service": service,
                }
            )
        return prepared

    def _create_requirements(self, plugin, prepared: list[dict]) -> None:
        """Persist plugin dependencies without activating services."""
        self.requirements.bulk_create_for_plugin(plugin, prepared)

    def _replace_requirements(self, plugin, prepared: list[dict]) -> None:
        """Replace requirement-to-service mappings without owning services."""
        self.requirements.delete_for_plugin(plugin.id)
        self._create_requirements(plugin, prepared)
