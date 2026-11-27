"""Runtime-focused snapshot of effective workspace plugins.

An *effective* plugin is opted in per workspace (``WorkspacePluginActivation``)
AND either a personal plugin of the workspace owner, or a built-in or
organization plugin of the workspace's org that is ``enabled`` +
``published`` and released org-wide (``OrgPluginActivation``). Withdrawing
the release makes an existing workspace activation ineffective (rows are
kept, per domain).

No secret values are stored in the snapshot; credential resolution
(see :mod:`apps.plugins.runtime`) decrypts server-side at run start.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

from .skill_documents import render_skill_markdown, skill_identifier


@dataclass(frozen=True)
class DesktopResourceSnapshot:
    """Immutable managed display policy."""

    activation: Literal["server_start", "first_tool"] = "server_start"


@dataclass(frozen=True)
class PluginResourcesSnapshot:
    """Immutable declared resources."""

    desktop: DesktopResourceSnapshot | None = None


@dataclass(frozen=True)
class PluginSkillSnapshot:
    """One ordered skill of an effective plugin."""

    id: uuid.UUID
    name: str
    slug: str
    body: str
    position: int
    description: str = ""


@dataclass(frozen=True)
class SkillEntry:
    """A plugin skill as agents address it: unique identifier + content."""

    identifier: str
    name: str
    plugin_name: str
    description: str
    body: str

    def markdown(self) -> str:
        """The skill as a SKILL.md document."""
        return render_skill_markdown(
            identifier=self.identifier, description=self.description, body=self.body
        )


class McpServerUnavailableError(RuntimeError):
    """The MCP server does not run on the workspace's guest OS."""


@dataclass(frozen=True)
class McpServerGuestOverride:
    """How one guest OS deviates from an MCP server's base definition.

    ``args`` replaces the argument list; a non-empty ``unavailable`` reason
    means the server cannot run on that guest at all.
    """

    args: tuple[str, ...] | None = None
    unavailable: str = ""


@dataclass(frozen=True)
class PluginMcpServerSnapshot:
    """One MCP server definition of an effective plugin.

    ``env``/``headers`` values may still contain
    ``{{credential.KEY}}`` placeholders; rendering happens in
    :func:`apps.plugins.runtime.render_server_config`. ``guest_overrides``
    maps a guest OS (``apps.runners.guest_os``) to its deviations; resolve
    them with :meth:`for_guest` before starting the server.
    """

    id: uuid.UUID
    name: str
    slug: str
    transport: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str = "/workspace"
    resources: PluginResourcesSnapshot = field(default_factory=PluginResourcesSnapshot)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    auth_type: str = "none"
    oauth_requirement_key: str = ""
    oauth_service_id: uuid.UUID | None = None
    startup_timeout_seconds: int = 30
    request_timeout_seconds: int = 60
    tool_policy: dict = field(default_factory=dict)
    runtime_artifact: dict = field(default_factory=dict)
    guest_overrides: dict[str, McpServerGuestOverride] = field(default_factory=dict)
    updated_at: str = ""

    def runs_on(self, guest_os: str) -> bool:
        """Whether the server can run on *guest_os*."""
        override = self.guest_overrides.get(guest_os)
        return override is None or not override.unavailable

    def for_guest(self, guest_os: str) -> PluginMcpServerSnapshot:
        """Return this server as it runs on *guest_os*.

        Raises:
            McpServerUnavailableError: If the server does not run there.
        """
        override = self.guest_overrides.get(guest_os)
        if override is None:
            return self
        if override.unavailable:
            raise McpServerUnavailableError(override.unavailable)
        return replace(
            self,
            args=self.args if override.args is None else override.args,
            guest_overrides={},
        )


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
    credential_type: str = "env"
    #: The credential the plugin itself names (if usable in this workspace).
    credential_id: uuid.UUID | None = None


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
    scope: str = "builtin"
    updated_at: str = ""


@dataclass(frozen=True)
class WorkspacePluginSnapshot:
    """Immutable runtime snapshot for one workspace run."""

    workspace_id: uuid.UUID
    organization_id: uuid.UUID
    plugins: tuple[EffectivePluginSnapshot, ...] = ()

    @property
    def skill_entries(self) -> list[SkillEntry]:
        """Skills of all effective plugins with unique identifiers, in order.

        The skill slug is the identifier; a slug used by several plugins is
        prefixed with the plugin slug, and any remaining clash gets a number.
        """
        counts: dict[str, int] = {}
        for plugin in self.plugins:
            for skill in plugin.skills:
                key = skill_identifier(skill.slug)
                counts[key] = counts.get(key, 0) + 1
        entries: list[SkillEntry] = []
        used: set[str] = set()
        for plugin in self.plugins:
            for skill in plugin.skills:
                key = skill_identifier(skill.slug)
                if counts[key] > 1:
                    key = skill_identifier(f"{plugin.slug}-{skill.slug}")
                base, suffix = key, 2
                while key in used:
                    key = skill_identifier(f"{base[:58]}-{suffix}")
                    suffix += 1
                used.add(key)
                entries.append(
                    SkillEntry(
                        identifier=key,
                        name=skill.name,
                        plugin_name=plugin.name,
                        description=skill.description,
                        body=skill.body,
                    )
                )
        return entries

    @property
    def plugin_skills(self) -> list[str]:
        """SKILL.md documents of all effective plugin skills, in order."""
        return [entry.markdown() for entry in self.skill_entries]


@dataclass(frozen=True)
class PreparedPluginRuntime:
    """A snapshot plus its resolved credentials for one harness run.

    Built exactly once per harness run by
    :func:`apps.plugins.runtime.prepare_workspace_runtime` (sync ORM
    context) and consumed by :meth:`McpRuntime.setup` without a second
    ORM/decrypt round. The ``plaintexts`` map (``plugin_id ->
    {requirement key: secret}``) is ``repr=False`` so secrets never
    surface in logs, tracebacks, or persisted events.

    ``oauth_credentials`` may be empty. That means resolution already
    ran and no connected OAuth credential matched. Emptiness is not a
    signal to resolve again.
    """

    snapshot: WorkspacePluginSnapshot
    plaintexts: dict[uuid.UUID, dict[str, str]] = field(
        default_factory=dict, repr=False
    )
    oauth_credentials: dict[uuid.UUID, uuid.UUID] = field(
        default_factory=dict, repr=False
    )

    def plaintexts_for(self, plugin_id: uuid.UUID) -> dict[str, str]:
        """Return the decrypted requirement map for one plugin (copy)."""
        return dict(self.plaintexts.get(plugin_id, {}))
