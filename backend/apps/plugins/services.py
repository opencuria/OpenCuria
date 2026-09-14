"""
Service layer for the plugins app.

All business logic lives here. The API layer delegates to these methods;
repositories encapsulate all ORM access.
"""

from __future__ import annotations

import logging
import re
import uuid

from django.db import transaction
from django.utils.text import slugify

from common.exceptions import ConflictError, NotFoundError

from .models import PluginTransport
from .repositories import (
    OrgPluginActivationRepository,
    PluginCredentialRequirementRepository,
    PluginMcpServerRepository,
    PluginRepository,
    PluginSkillRepository,
    WorkspacePluginActivationRepository,
)

logger = logging.getLogger(__name__)

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
PLAYWRIGHT_MCP_ARGS = [
    "-y",
    "@playwright/mcp@latest",
    "--headless",
    "--isolated",
    "--no-sandbox",
    "--executable-path",
    "/usr/bin/google-chrome-stable",
    "--output-dir",
    "/workspace/.opencuria/playwright",
]


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
        "startup_timeout_seconds": startup_timeout,
        "request_timeout_seconds": request_timeout,
    }


# ---------------------------------------------------------------------------
# Plugin service
# ---------------------------------------------------------------------------


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
        """Return visible plugins with components + org readiness summary.

        Readiness here is org-scoped on purpose: it reports whether an
        *org credential* exists for each required service (personal
        credentials cannot be inspected org-wide). Workspace activation
        paths check the workspace-attached credentials (personal or org)
        instead and must not rely on this summary.
        """
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
        from apps.credentials.services import CredentialServiceSvc

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

        # Validate + prepare credential requirements before writing anything.
        prepared_requirements = self._prepare_requirement_inputs(
            credential_requirements or [],
            org_id=org_id,
            credential_svc=CredentialServiceSvc(),
            service_repo=CredentialServiceRepository,
        )

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
        from apps.credentials.services import CredentialServiceSvc

        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None or plugin.organization_id != org_id:
            raise NotFoundError("Plugin", str(plugin_id))

        fields: dict = {}
        if name is not None:
            fields["name"] = validate_name(name, field="Plugin name")
        if slug is not None:
            normalized = normalize_slug(slug, field="Plugin slug")
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
        prepared_requirements = None
        if credential_requirements is not None:
            prepared_requirements = self._prepare_requirement_inputs(
                credential_requirements,
                org_id=org_id,
                credential_svc=CredentialServiceSvc(),
                service_repo=CredentialServiceRepository,
                existing_plugin=plugin,
            )

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
        """Delete an org-owned plugin.

        Exclusively-created service definitions are removed as well, but only
        when no credentials reference them; otherwise deletion is blocked
        with a 409 ConflictError.
        """
        from apps.credentials.repositories import (
            CredentialRepository,
            CredentialServiceRepository,
        )

        plugin = self.plugins.get_visible_by_id(plugin_id, org_id)
        if plugin is None or plugin.organization_id != org_id:
            raise NotFoundError("Plugin", str(plugin_id))

        owned_requirements = list(
            self.requirements.list_for_plugin(plugin.id).filter(
                plugin_owned_service=True
            )
        )
        owned_service_ids = [r.credential_service_id for r in owned_requirements]
        if CredentialRepository.service_ids_with_credentials(owned_service_ids):
            raise ConflictError(
                "Plugin cannot be deleted while credentials reference "
                "its credential services",
                code="plugin_credentials_in_use",
            )
        # Delete requirements first (PROTECT on the service FK would block
        # plugin deletion otherwise), then owned service definitions, then
        # the plugin row itself.
        self.requirements.delete_for_plugin(plugin.id)
        CredentialServiceRepository.delete_org_services(org_id, owned_service_ids)
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
        attached_service_ids = set(
            workspace.credentials.all().values_list("service_id", flat=True)
        )

        result = []
        for plugin in plugins:
            reqs = list(self.requirements.list_for_plugin(plugin.id))
            missing = [
                {
                    "key": r.key,
                    "service_id": str(r.credential_service_id),
                    "service_slug": r.credential_service.slug,
                }
                for r in reqs
                if r.required and r.credential_service_id not in attached_service_ids
            ]
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

    @transaction.atomic
    def set_workspace_plugins(
        self,
        *,
        workspace,
        org_id: uuid.UUID,
        user,
        plugin_ids: list[uuid.UUID],
    ) -> list[dict]:
        """Replace workspace activations atomically.

        Only org-enabled, visible and published plugins are accepted.
        Required credential services must be attached to the workspace,
        otherwise a 409 ConflictError with a machine-readable payload is
        raised. An empty list clears all activations.
        """
        desired = list(dict.fromkeys(plugin_ids))
        if not desired:
            self.workspace_activations.replace_for_workspace(
                workspace, [], enabled_by=user
            )
            return self.list_workspace_plugins(workspace=workspace, org_id=org_id)

        visible = {
            p.id: p for p in self.plugins.list_visible_to_org(org_id) if p.id in desired
        }
        missing_plugins = [pid for pid in desired if pid not in visible]
        if missing_plugins:
            raise NotFoundError(
                "Plugin", ", ".join(str(pid) for pid in missing_plugins)
            )
        enabled_ids = self.org_activations.enabled_plugin_ids(org_id)
        not_enabled = [pid for pid in desired if pid not in enabled_ids]
        if not_enabled:
            raise ConflictError(
                "Plugins are not enabled for this organization: "
                + ", ".join(str(pid) for pid in not_enabled),
                code="plugin_not_available",
            )
        not_releasable = [
            str(pid)
            for pid, plugin in visible.items()
            if not (plugin.enabled and plugin.published)
        ]
        if not_releasable:
            raise ConflictError(
                "Plugins are not published: " + ", ".join(not_releasable),
                code="plugin_not_available",
            )

        attached_service_ids = set(
            workspace.credentials.all().values_list("service_id", flat=True)
        )
        gaps: list[dict] = []
        for plugin_id in desired:
            for req in self.requirements.list_for_plugin(plugin_id):
                if req.required and req.credential_service_id not in (
                    attached_service_ids
                ):
                    gaps.append(
                        {
                            "plugin_id": str(plugin_id),
                            "key": req.key,
                            "service_id": str(req.credential_service_id),
                        }
                    )
        if gaps:
            raise ConflictError(
                "Missing required credentials for workspace plugins: "
                + ", ".join(
                    f"{g['plugin_id']}:{g['key']}->{g['service_id']}" for g in gaps
                ),
                code="missing_plugin_credentials",
            )

        self.workspace_activations.replace_for_workspace(
            workspace, desired, enabled_by=user
        )
        logger.info(
            "Workspace plugins updated: ws=%s plugins=%s",
            workspace.id,
            [str(pid) for pid in desired],
        )
        return self.list_workspace_plugins(workspace=workspace, org_id=org_id)

    # -- Credential removal guard --------------------------------------------

    def validate_workspace_credential_removal(
        self,
        *,
        workspace,
        org_id: uuid.UUID,
        remaining_service_ids: set[uuid.UUID],
    ) -> list[dict]:
        """Return blocking gaps if removing credentials would break plugins.

        Reusable by the runner workspace-update path and the credential
        delete path: only service IDs are inspected, never secret values.
        Returns a list of machine-readable gap dicts (empty when removal
        is safe).
        """
        ws_enabled = self.workspace_activations.enabled_plugin_ids(workspace.id)
        if not ws_enabled:
            return []
        org_enabled = self.org_activations.enabled_plugin_ids(org_id)
        effective = [pid for pid in ws_enabled if pid in org_enabled]
        if not effective:
            return []
        # Only *effective definitions* gate removal: a disabled or
        # unpublished plugin is filtered from the runtime snapshot and
        # from the workspace list, so it must not block credential
        # detachment either.
        visible_effective = {
            p.id
            for p in self.plugins.list_visible_to_org(org_id)
            if p.id in set(effective) and p.enabled and p.published
        }
        if not visible_effective:
            return []
        gaps: list[dict] = []
        for plugin_id in visible_effective:
            for req in self.requirements.list_for_plugin(plugin_id):
                if req.required and req.credential_service_id not in (
                    remaining_service_ids
                ):
                    gaps.append(
                        {
                            "plugin_id": str(plugin_id),
                            "key": req.key,
                            "service_id": str(req.credential_service_id),
                        }
                    )
        return gaps

    def blocking_plugin_gaps_for_credential(
        self,
        credential,
        *,
        org_id: uuid.UUID,
    ) -> list[dict]:
        """Return effective-plugin gaps if *credential* were deleted.

        Inspects every workspace the credential is attached to (org
        credentials may fan out to many workspaces) and reuses
        :meth:`validate_workspace_credential_removal` with the service
        removed from the remaining set. Personal credentials only ever
        affect the owner's workspaces that carry this exact row. Only
        service IDs are inspected, never secret values.
        """
        from apps.runners.repositories import WorkspaceRepository

        workspace_ids = WorkspaceRepository.list_ids_for_credential(credential.id)
        if not workspace_ids:
            return []
        gaps: list[dict] = []
        for workspace_id in workspace_ids:
            workspace = WorkspaceRepository.get_by_id(workspace_id)
            if workspace is None:
                continue
            # Each workspace is evaluated under its own real org
            # (``workspace.runner.organization_id``): a personal
            # credential may hang on workspaces of several orgs, and the
            # request header org must never decide for a foreign org's
            # workspace.
            runner_org_id = getattr(
                getattr(workspace, "runner", None), "organization_id", None
            )
            if runner_org_id is None:
                continue
            remaining = {
                cred.service_id
                for cred in workspace.credentials.all()
                if cred.id != credential.id
            }
            for gap in self.validate_workspace_credential_removal(
                workspace=workspace,
                org_id=runner_org_id,
                remaining_service_ids=remaining,
            ):
                if gap["service_id"] == str(credential.service_id):
                    gaps.append({**gap, "workspace_id": str(workspace_id)})
        return gaps

    # -- Internal helpers ----------------------------------------------------

    def _serialize(self, plugin, *, org_id: uuid.UUID) -> dict:
        """Serialize a plugin with components + org readiness summary.

        ``credential_readiness`` is intentionally org-scoped: ``ready``
        means an *org credential* exists for every required service
        (``missing_required_service_ids`` lists org gaps). A workspace
        may still be ready via workspace-attached personal credentials —
        the workspace serializers check attachments directly.
        """
        from apps.credentials.repositories import CredentialRepository

        skills = list(self.skills.list_for_plugin(plugin.id))
        mcps = list(self.mcp_servers.list_for_plugin(plugin.id))
        reqs = list(self.requirements.list_for_plugin(plugin.id))
        org_enabled = self.org_activations.is_enabled(org_id, plugin.id)
        required_ids = [r.credential_service_id for r in reqs if r.required]
        attached_service_ids = CredentialRepository.org_service_ids_with_credentials(
            org_id, required_ids
        )
        missing = [sid for sid in required_ids if sid not in attached_service_ids]
        return {
            "id": plugin.id,
            "name": plugin.name,
            "slug": plugin.slug,
            "description": plugin.description,
            "enabled": plugin.enabled,
            "published": plugin.published,
            "organization_id": plugin.organization_id,
            "is_global": plugin.organization_id is None,
            "org_enabled": org_enabled,
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
                    "plugin_owned_service": r.plugin_owned_service,
                }
                for r in reqs
            ],
            "credential_readiness": {
                "required_service_ids": required_ids,
                "missing_required_service_ids": missing,
                "ready": not missing,
            },
            "created_at": plugin.created_at,
            "updated_at": plugin.updated_at,
        }

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
        credential_svc,
        service_repo,
        existing_plugin=None,
    ) -> list[dict]:
        """Validate requirement inputs and resolve/create services."""
        prepared: list[dict] = []
        seen_keys: set[str] = set()
        seen_services: set[uuid.UUID] = set()
        for raw in raw_requirements:
            item = dict(raw) if isinstance(raw, dict) else {}
            # Support both service shapes (nested objects from API schemas
            # arrive as dicts here).
            svc_input = item.get("credential_service") or {}
            if not isinstance(svc_input, dict):
                raise ValueError("credential_service must be an object")
            key = normalize_requirement_key(item.get("key", ""))
            if key in seen_keys:
                raise ConflictError(f"Duplicate credential requirement key '{key}'")
            seen_keys.add(key)
            required = bool(item.get("required", True))
            description = (item.get("description") or "").strip()
            if len(description) > 2000:
                raise ValueError("Credential requirement description is too long")

            service_id = svc_input.get("service_id")
            if service_id is not None:
                try:
                    service_uuid = uuid.UUID(str(service_id))
                except ValueError:
                    raise ValueError(
                        "credential_service.service_id must be a UUID"
                    ) from None
                service = service_repo.get_visible_by_id(service_uuid, org_id)
                if service is None:
                    raise NotFoundError("CredentialService", str(service_id))
                if (
                    existing_plugin is not None
                    and getattr(service, "organization_id", None) is not None
                    and str(service.organization_id) != str(org_id)
                ):
                    raise NotFoundError("CredentialService", str(service_id))
                plugin_owned = False
            else:
                try:
                    service = credential_svc.create_service(
                        name=(svc_input.get("name") or key).strip() or key,
                        slug=(svc_input.get("slug") or key).strip() or key,
                        description=(svc_input.get("description") or "").strip(),
                        credential_type=(svc_input.get("credential_type") or "env"),
                        env_var_name=svc_input.get("env_var_name", ""),
                        target_path=svc_input.get("target_path", ""),
                        label=svc_input.get("label", ""),
                        organization_id=org_id,
                    )
                except ValueError as exc:
                    # Fresh-service slug collisions surface as 409 (the
                    # API layer maps ConflictError), matching duplicate
                    # plugin slugs.
                    raise ConflictError(str(exc), code="conflict") from exc
                plugin_owned = True
            if service.id in seen_services:
                raise ConflictError("Duplicate credential service in requirements")
            seen_services.add(service.id)
            prepared.append(
                {
                    "key": key,
                    "description": description,
                    "required": required,
                    "service": service,
                    "plugin_owned_service": plugin_owned,
                }
            )
        return prepared

    def _create_requirements(self, plugin, prepared: list[dict]) -> None:
        """Persist prepared requirements for a plugin."""
        from apps.credentials.repositories import (
            OrgCredentialServiceActivationRepository,
        )

        self.requirements.bulk_create_for_plugin(plugin, prepared)
        # Auto-activate referenced services for the plugin's org so the org
        # can immediately use them.
        if plugin.organization_id is not None:
            OrgCredentialServiceActivationRepository.ensure_activated(
                plugin.organization_id,
                [item["service"].id for item in prepared],
            )

    def _replace_requirements(self, plugin, prepared: list[dict]) -> None:
        """Replace requirements, pruning orphaned plugin-owned services."""
        from apps.credentials.repositories import (
            CredentialRepository,
            CredentialServiceRepository,
        )

        previous = list(self.requirements.list_for_plugin(plugin.id))
        previous_owned_ids = {
            r.credential_service_id for r in previous if r.plugin_owned_service
        }
        new_service_ids = {item["service"].id for item in prepared}
        self.requirements.delete_for_plugin(plugin.id)
        # Preserve the plugin-owned marker for previously owned services
        # that are re-referenced via ``service_id`` (fresh inputs clear
        # the flag). Without this, a re-referenced owned service would
        # lose its marker and later leak as an undeletable orphan.
        for item in prepared:
            if (
                not item["plugin_owned_service"]
                and item["service"].id in previous_owned_ids
            ):
                item["plugin_owned_service"] = True
        self._create_requirements(plugin, prepared)
        # Prune previously owned services that are no longer referenced,
        # unless credentials still point at them.
        orphan_ids = list(previous_owned_ids - new_service_ids)
        if orphan_ids:
            blocking = CredentialRepository.org_service_ids_with_credentials(
                plugin.organization_id, orphan_ids
            )
            if blocking:
                raise ConflictError(
                    "Credential requirements cannot be replaced while "
                    "credentials reference their services",
                    code="plugin_credentials_in_use",
                )
            # Never delete services still required by other plugins.
            still_used = self.requirements.service_ids_used_by_other_org_plugins(
                org_id=plugin.organization_id,
                excluded_plugin_id=plugin.id,
            )
            deletable = [sid for sid in orphan_ids if sid not in still_used]
            CredentialServiceRepository.delete_org_services(
                plugin.organization_id, deletable
            )
