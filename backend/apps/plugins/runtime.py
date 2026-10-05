"""Server-side runtime resolution for effective workspace plugins.

Builds the immutable :class:`WorkspacePluginSnapshot` (effective plugins
only) and renders ``{{credential.KEY}}`` placeholders from the
workspace-attached credentials (personal or org — both already validated
at attach time).

Secrets are decrypted here and never leave the backend process towards
the API: rendered env/headers go only to the workspace-local MCP
transport. No secret values are logged or persisted.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import replace

from common.utils import decrypt_value

from .repositories import (
    OrgPluginActivationRepository,
    PluginCredentialRequirementRepository,
    PluginMcpServerRepository,
    PluginRepository,
    PluginSkillRepository,
    WorkspacePluginActivationRepository,
)
from .runtime_snapshot import (
    DesktopResourceSnapshot,
    EffectivePluginSnapshot,
    PluginMcpServerSnapshot,
    PluginRequirementSnapshot,
    PluginResourcesSnapshot,
    PluginSkillSnapshot,
    WorkspacePluginSnapshot,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}")


class PluginCredentialConfigError(ValueError):
    """Raised when effective plugin credentials are misconfigured.

    Subclasses :class:`ValueError` so the harness run fails with a clear
    ``error`` finish reason through the existing failure path.
    """


def _workspace_org_id(workspace) -> uuid.UUID | None:
    """Return the workspace org id from the loaded relation (no queries).

    The caller must pass a workspace with ``runner`` (and
    ``runner.organization_id``) already loaded — e.g. via
    :meth:`WorkspaceRepository.get_by_id`, which select-relateds the
    runner. A missing relation fails closed (``None``) instead of
    issuing a lazy ORM query from the runtime layer.
    """
    runner = getattr(workspace, "runner", None)
    return getattr(runner, "organization_id", None)


def _verify_workspace_org(workspace, org_id: uuid.UUID) -> uuid.UUID:
    """Return the workspace org id, failing closed on mismatch.

    The workspace org is ``workspace.runner.organization_id`` (the
    backend source of truth, loaded by the caller via the repository).
    A mismatch means the caller mixed tenants: refuse instead of
    leaking cross-org plugins or credentials.
    """
    runner_org_id = _workspace_org_id(workspace)
    if runner_org_id is None or str(runner_org_id) != str(org_id):
        raise PluginCredentialConfigError(
            "Workspace organization mismatch; refusing plugin resolution."
        )
    return runner_org_id


def _service_visible_to_org(service, org_id: uuid.UUID) -> bool:
    """Return True when *service* is global or owned by *org_id*."""
    service_org = getattr(service, "organization_id", None)
    return service_org is None or str(service_org) == str(org_id)


def placeholder_keys(value: str) -> list[str]:
    """Return all ``{{credential.KEY}}`` keys referenced in *value*."""
    if not isinstance(value, str):
        return []
    return _PLACEHOLDER_RE.findall(value)


def build_workspace_plugin_snapshot(
    *, workspace, org_id: uuid.UUID
) -> WorkspacePluginSnapshot:
    """Return the immutable snapshot of effective plugins for *workspace*.

    Effective = visible to *org_id* (global or org-owned) AND
    ``enabled`` + ``published`` AND org-activated AND workspace-activated.
    Skills/servers/requirements are ordered deterministically (by name /
    position / key).
    """
    workspace_org_id = _verify_workspace_org(workspace, org_id)
    visible = [
        plugin
        for plugin in PluginRepository.list_visible_to_org(workspace_org_id)
        if plugin.enabled and plugin.published
    ]
    if not visible:
        return WorkspacePluginSnapshot(
            workspace_id=workspace.id, organization_id=org_id, plugins=()
        )
    org_enabled = OrgPluginActivationRepository.enabled_plugin_ids(org_id)
    ws_enabled = WorkspacePluginActivationRepository.enabled_plugin_ids(workspace.id)
    effective = [p for p in visible if p.id in org_enabled and p.id in ws_enabled]
    snapshots: list[EffectivePluginSnapshot] = []
    for plugin in effective:
        skills = tuple(
            PluginSkillSnapshot(
                id=skill.id,
                name=skill.name,
                slug=skill.slug,
                body=skill.body,
                position=skill.position,
            )
            for skill in PluginSkillRepository.list_for_plugin(plugin.id)
        )
        plugin_requirements = list(
            PluginCredentialRequirementRepository.list_for_plugin(plugin.id)
        )
        service_by_key = {
            req.key: req.credential_service_id for req in plugin_requirements
        }
        servers = tuple(
            PluginMcpServerSnapshot(
                id=server.id,
                name=server.name,
                slug=server.slug,
                transport=server.transport,
                command=server.command or "",
                args=tuple(server.args or []),
                cwd=server.cwd or "/workspace",
                resources=PluginResourcesSnapshot(
                    desktop=(
                        DesktopResourceSnapshot(
                            activation=server.resources["desktop"].get(
                                "activation", "server_start"
                            )
                        )
                        if server.resources.get("desktop") is not None
                        else None
                    ),
                ),
                env=dict(server.env or {}),
                url=server.url or "",
                headers=dict(server.headers or {}),
                auth_type=server.auth_type,
                oauth_requirement_key=server.oauth_requirement_key,
                oauth_service_id=service_by_key.get(server.oauth_requirement_key),
                startup_timeout_seconds=server.startup_timeout_seconds,
                request_timeout_seconds=server.request_timeout_seconds,
            )
            for server in PluginMcpServerRepository.list_for_plugin(plugin.id)
        )
        requirements = tuple(
            PluginRequirementSnapshot(
                id=req.id,
                key=req.key,
                required=bool(req.required),
                description=req.description or "",
                service_id=req.credential_service_id,
                service_slug=req.credential_service.slug,
                service_name=req.credential_service.name,
                credential_type=req.credential_service.credential_type,
            )
            for req in plugin_requirements
        )
        snapshots.append(
            EffectivePluginSnapshot(
                id=plugin.id,
                name=plugin.name,
                slug=plugin.slug,
                description=plugin.description or "",
                organization_id=plugin.organization_id,
                is_global=plugin.organization_id is None,
                skills=skills,
                mcp_servers=servers,
                requirements=requirements,
            )
        )
    return WorkspacePluginSnapshot(
        workspace_id=workspace.id, organization_id=org_id, plugins=tuple(snapshots)
    )


def _workspace_credentials_by_service(
    workspace, *, org_id: uuid.UUID
) -> dict[uuid.UUID, object]:
    """Map attached ``service_id -> Credential`` (fail-closed ownership).

    Only credentials owned by ``workspace.created_by`` (personal) or by
    the workspace org (org) with a service visible in this org count.
    Directly attached foreign rows (DB corruption / cross-org M2M leak)
    are ignored, so they surface as missing-required gaps.
    """
    attached = list(workspace.credentials.all().select_related("service"))
    by_service: dict[uuid.UUID, object] = {}
    owner_id = getattr(workspace, "created_by_id", None)
    for credential in attached:
        service = getattr(credential, "service", None)
        if service is None or not _service_visible_to_org(service, org_id):
            continue
        personal = getattr(credential, "user_id", None) is not None
        owned_org = getattr(credential, "organization_id", None)
        if personal:
            if owner_id is None or str(credential.user_id) != str(owner_id):
                continue
        elif owned_org is None or str(owned_org) != str(org_id):
            continue
        else:
            pass
        by_service.setdefault(credential.service_id, credential)
    return by_service


def resolve_plugin_plaintexts(
    plugin: EffectivePluginSnapshot,
    credentials_by_service: dict[uuid.UUID, object],
) -> dict[str, str]:
    """Decrypt requirement key -> plaintext for one effective plugin.

    Only ``workspace.credentials`` rows are consulted (personal or org).
    Raises :class:`PluginCredentialConfigError` when a *required*
    requirement has no attached credential.
    """
    by_key: dict[str, str] = {}
    req_by_key = {req.key: req for req in plugin.requirements}
    for key, req in req_by_key.items():
        credential = credentials_by_service.get(req.service_id)
        if credential is None or credential.service.credential_type == "mcp_oauth":
            if req.required:
                raise PluginCredentialConfigError(
                    f"Plugin '{plugin.slug}' is missing required credential "
                    f"'{key}' (service '{req.service_slug}'); "
                    "attach it to the workspace before running."
                )
            continue
        try:
            plaintext = decrypt_value(credential.encrypted_value)  # type: ignore[attr-defined]
        except Exception:
            raise PluginCredentialConfigError(
                f"Plugin '{plugin.slug}' credential '{key}' cannot be decrypted."
            ) from None
        by_key[key] = plaintext
    return by_key


def render_mapping(
    mapping: dict[str, str],
    plaintexts: dict[str, str],
    requirement_keys: set[str],
    *,
    plugin_slug: str,
    field: str,
) -> dict[str, str]:
    """Render ``{{credential.KEY}}`` placeholders in one mapping.

    Literals are preserved. Unknown keys and referenced-but-missing
    credentials (required or optional) raise
    :class:`PluginCredentialConfigError`. Unreferenced mappings are
    still validated (unknown keys raise) but never injected by the
    caller.
    """
    rendered: dict[str, str] = {}
    for name, raw in mapping.items():
        keys = placeholder_keys(raw)
        for key in keys:
            if key not in requirement_keys:
                raise PluginCredentialConfigError(
                    f"Plugin '{plugin_slug}' {field} entry '{name}' references "
                    f"unknown credential '{key}'."
                )
            if key not in plaintexts:
                raise PluginCredentialConfigError(
                    f"Plugin '{plugin_slug}' {field} entry '{name}' references "
                    f"credential '{key}' which is not attached to the workspace."
                )

        def _replace(match: re.Match[str]) -> str:
            return plaintexts[match.group(1)]

        rendered[name] = _PLACEHOLDER_RE.sub(_replace, raw)
    return rendered


def render_server_config(
    plugin: EffectivePluginSnapshot,
    server: PluginMcpServerSnapshot,
    plaintexts: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Render ``(env, headers)`` for one server; validates both mappings.

    The transport layer injects only the mapping it uses (stdio -> env,
    HTTP/SSE -> headers); the unused mapping is still validated so typos
    and missing credentials fail closed at run start.
    """
    oauth_keys = {
        req.key for req in plugin.requirements if req.credential_type == "mcp_oauth"
    }
    if any(
        key in oauth_keys
        for mapping in (server.env or {}, server.headers or {})
        for value in mapping.values()
        for key in placeholder_keys(value)
    ):
        raise PluginCredentialConfigError(
            f"Plugin '{plugin.slug}' OAuth requirements cannot be used as placeholders."
        )
    requirement_keys = {req.key for req in plugin.requirements}
    rendered_env = render_mapping(
        dict(server.env or {}),
        plaintexts,
        requirement_keys,
        plugin_slug=plugin.slug,
        field="env",
    )
    rendered_headers = render_mapping(
        dict(server.headers or {}),
        plaintexts,
        requirement_keys,
        plugin_slug=plugin.slug,
        field="headers",
    )
    return rendered_env, rendered_headers


def resolve_runtime_oauth_credentials(
    snapshot: WorkspacePluginSnapshot, *, workspace
) -> dict[uuid.UUID, uuid.UUID]:
    """Resolve only matching OAuth credential row IDs; no token refresh here."""
    org_id = _verify_workspace_org(workspace, snapshot.organization_id)
    attached = _workspace_credentials_by_service(workspace, org_id=org_id)
    resolved: dict[uuid.UUID, uuid.UUID] = {}
    for plugin in snapshot.plugins:
        plugin_oauth_requirements = {
            requirement.key: requirement
            for requirement in plugin.requirements
            if requirement.credential_type == "mcp_oauth" and requirement.required
        }
        connected_keys = set()
        for server in plugin.mcp_servers:
            if server.auth_type != "oauth":
                continue
            requirement = next(
                (
                    item
                    for item in plugin.requirements
                    if item.key == server.oauth_requirement_key
                ),
                None,
            )
            credential = attached.get(requirement.service_id) if requirement else None
            if (
                credential is not None
                and credential.service.credential_type == "mcp_oauth"
            ):
                from apps.credentials.mcp_oauth import oauth_credential_status

                if (
                    credential.oauth_server_url == server.url
                    and credential.service.oauth_server_url == server.url
                    and oauth_credential_status(credential)["connected"]
                ):
                    resolved[server.id] = credential.id
                    connected_keys.add(server.oauth_requirement_key)
        for key, requirement in plugin_oauth_requirements.items():
            if key not in connected_keys:
                raise PluginCredentialConfigError(
                    f"Plugin '{plugin.slug}' requires connected OAuth credential "
                    f"'{key}' (service '{requirement.service_slug}'); connect or "
                    "reconnect it before running."
                )
    return resolved


def resolve_runtime_credentials(
    snapshot: WorkspacePluginSnapshot, *, workspace
) -> dict[uuid.UUID, dict[str, str]]:
    """Decrypt credentials for all effective plugins.

    Returns ``plugin_id -> {requirement key: plaintext}``. Raises
    :class:`PluginCredentialConfigError` on the first gap so the run
    fails before any provider call. Credentials for non-effective or
    foreign services are ignored.
    """
    org_id = _verify_workspace_org(workspace, snapshot.organization_id)
    credentials_by_service = _workspace_credentials_by_service(workspace, org_id=org_id)
    resolved: dict[uuid.UUID, dict[str, str]] = {}
    for plugin in snapshot.plugins:
        # OAuth tokens are scoped and injected by the MCP transport only;
        # never decrypt them into plugin placeholder maps.
        oauth_keys = {
            server.oauth_requirement_key
            for server in plugin.mcp_servers
            if server.auth_type == "oauth"
        }
        ordinary_requirements = tuple(
            req
            for req in plugin.requirements
            if req.key not in oauth_keys and req.credential_type != "mcp_oauth"
        )
        safe_credentials = {
            sid: cred
            for sid, cred in credentials_by_service.items()
            if cred.service.credential_type != "mcp_oauth"
        }
        resolved[plugin.id] = resolve_plugin_plaintexts(
            replace(plugin, requirements=ordinary_requirements), safe_credentials
        )
    logger.info(
        "plugin runtime credentials resolved for workspace %s (%d plugins)",
        snapshot.workspace_id,
        len(snapshot.plugins),
    )
    return resolved
