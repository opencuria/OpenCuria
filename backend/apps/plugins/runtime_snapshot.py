"""Runtime-focused snapshot of effective workspace plugins.

An *effective* plugin is visible to the workspace's org (global or
org-owned by the workspace org), ``enabled`` + ``published``, released
org-wide (``OrgPluginActivation`` row exists) AND opted in per workspace
(``WorkspacePluginActivation`` row exists). Org deactivation makes an
existing workspace activation ineffective (rows are kept, per domain).

No secret values are stored in the snapshot; credential resolution
(see :mod:`apps.plugins.runtime`) decrypts server-side at run start.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PluginSkillSnapshot:
    """One ordered skill fragment of an effective plugin."""

    id: uuid.UUID
    name: str
    slug: str
    body: str
    position: int


@dataclass(frozen=True)
class PluginMcpServerSnapshot:
    """One MCP server definition of an effective plugin.

    ``env``/``headers`` values may still contain
    ``{{credential.KEY}}`` placeholders; rendering happens in
    :func:`apps.plugins.runtime.render_server_config`.
    """

    id: uuid.UUID
    name: str
    slug: str
    transport: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    startup_timeout_seconds: int = 30
    request_timeout_seconds: int = 60


@dataclass(frozen=True)
class PluginRequirementSnapshot:
    """One plugin-wide credential requirement (no secret material)."""

    id: uuid.UUID
    key: str
    required: bool
    description: str
    service_id: uuid.UUID
    service_slug: str
    service_name: str


@dataclass(frozen=True)
class EffectivePluginSnapshot:
    """One effective plugin with ordered skills/servers/requirements."""

    id: uuid.UUID
    name: str
    slug: str
    description: str
    organization_id: uuid.UUID | None
    is_global: bool
    skills: tuple[PluginSkillSnapshot, ...] = ()
    mcp_servers: tuple[PluginMcpServerSnapshot, ...] = ()
    requirements: tuple[PluginRequirementSnapshot, ...] = ()


@dataclass(frozen=True)
class WorkspacePluginSnapshot:
    """Immutable runtime snapshot for one workspace run."""

    workspace_id: uuid.UUID
    organization_id: uuid.UUID
    plugins: tuple[EffectivePluginSnapshot, ...] = ()

    @property
    def plugin_skills(self) -> list[str]:
        """Ordered skill bodies (plugin name, then position)."""
        bodies: list[str] = []
        for plugin in self.plugins:
            for skill in plugin.skills:
                bodies.append(f"## {plugin.name} / {skill.name}\n{skill.body.strip()}")
        return bodies


@dataclass(frozen=True)
class PreparedPluginRuntime:
    """A snapshot plus its workspace and decrypted requirement plaintexts.

    Built exactly once per harness run by
    :meth:`HarnessService._prepare_mcp_snapshot_for_run` (sync ORM
    context) and consumed by :meth:`McpRuntime.setup` without a second
    ORM/decrypt round. The ``plaintexts`` map (``plugin_id ->
    {requirement key: secret}``) is ``repr=False`` so secrets never
    surface in logs, tracebacks, or persisted events; ``workspace`` is
    the already-loaded row (runner relation included).
    """

    snapshot: WorkspacePluginSnapshot = field(
        default_factory=lambda: WorkspacePluginSnapshot(
            workspace_id=uuid.uuid4(), organization_id=uuid.uuid4(), plugins=()
        )
    )
    workspace: Any = None
    plaintexts: dict[uuid.UUID, dict[str, str]] = field(
        default_factory=dict, repr=False
    )

    def plaintexts_for(self, plugin_id: uuid.UUID) -> dict[str, str]:
        """Return the decrypted requirement map for one plugin (copy)."""
        return dict(self.plaintexts.get(plugin_id, {}))


def prepared_is_empty(prepared: PreparedPluginRuntime | None) -> bool:
    """Return True when *prepared* carries no effective plugins."""
    if prepared is None:
        return True
    return not prepared.snapshot.plugins
