"""
Repository layer for the plugins app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid

from django.db.models import Q, QuerySet

from .models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    PluginSkill,
    WorkspacePluginActivation,
)

# ---------------------------------------------------------------------------
# Plugin Repository
# ---------------------------------------------------------------------------


def _replace_components(
    model: type[PluginSkill] | type[PluginMcpServer],
    plugin: Plugin,
    items: list[dict],
    *,
    fields: tuple[str, ...],
) -> list:
    """Replace a plugin's component rows, updating matched rows in place.

    An entry matches a stored row by its ``id`` first, then by ``slug``;
    unmatched rows are deleted and unmatched entries created.
    """
    existing = list(model.objects.filter(plugin_id=plugin.id))
    by_id = {str(row.id): row for row in existing}
    by_slug = {row.slug: row for row in existing}
    used: set[uuid.UUID] = set()
    matched = []
    for item in items:
        row = by_id.get(str(item.get("id") or ""))
        if row is None or row.id in used:
            row = by_slug.get(item["slug"])
        if row is not None and row.id in used:
            row = None
        if row is not None:
            used.add(row.id)
        matched.append((item, row))
    stale = [row.id for row in existing if row.id not in used]
    if stale:
        model.objects.filter(id__in=stale).delete()
    for item, row in matched:
        values = {key: item[key] for key in fields if key in item}
        if row is None:
            model.objects.create(plugin=plugin, **values)
            continue
        changed = [key for key, value in values.items() if getattr(row, key) != value]
        for key in changed:
            setattr(row, key, values[key])
        if changed:
            row.save(update_fields=[*changed, "updated_at"])
    return list(model.objects.filter(plugin_id=plugin.id))


def _visible(org_id: uuid.UUID, user_id: int | None) -> Q:
    """Built-in and organization plugins, plus *user_id*'s personal plugins."""
    shared = Q(owner__isnull=True) & (
        Q(organization__isnull=True) | Q(organization_id=org_id)
    )
    return shared | Q(owner_id=user_id) if user_id is not None else shared


class PluginRepository:
    """Data access for Plugin records."""

    @staticmethod
    def list_visible(org_id: uuid.UUID, user_id: int | None) -> QuerySet[Plugin]:
        """Return the plugins *user_id* sees in *org_id*, ordered by name.

        Without a user only built-in and organization plugins are returned.
        """
        return (
            Plugin.objects.filter(_visible(org_id, user_id))
            .select_related("organization", "created_by")
            .order_by("name")
        )

    @staticmethod
    def get_visible_by_id(
        plugin_id: uuid.UUID, org_id: uuid.UUID, user_id: int | None
    ) -> Plugin | None:
        """Fetch a plugin *user_id* sees in *org_id*."""
        return (
            Plugin.objects.filter(_visible(org_id, user_id), id=plugin_id)
            .select_related("organization", "created_by")
            .first()
        )

    @staticmethod
    def get_personal_by_slug(slug: str, owner_id: int) -> Plugin | None:
        """Fetch one of *owner_id*'s personal plugins by slug."""
        return Plugin.objects.filter(slug=slug, owner_id=owner_id).first()

    @staticmethod
    def get_by_id(plugin_id: uuid.UUID) -> Plugin | None:
        """Fetch a plugin by ID."""
        return (
            Plugin.objects.filter(id=plugin_id)
            .select_related("organization", "created_by")
            .first()
        )

    @staticmethod
    def get_org_by_slug(slug: str, org_id: uuid.UUID) -> Plugin | None:
        """Fetch an org-owned plugin of this org by slug."""
        return Plugin.objects.filter(slug=slug, organization_id=org_id).first()

    @staticmethod
    def create(
        *,
        name: str,
        slug: str,
        description: str,
        organization_id: uuid.UUID | None,
        created_by,
        owner=None,
        enabled: bool = True,
        published: bool = True,
    ) -> Plugin:
        """Create a plugin record (personal when *owner* is given)."""
        return Plugin.objects.create(
            name=name,
            slug=slug,
            description=description,
            organization_id=organization_id,
            owner=owner,
            created_by=created_by,
            enabled=enabled,
            published=published,
        )

    @staticmethod
    def update(plugin: Plugin, **fields) -> Plugin:
        """Update mutable plugin metadata fields."""
        allowed = {"name", "slug", "description", "enabled", "published"}
        update_fields = ["updated_at"]
        for key, value in fields.items():
            if key in allowed and value is not None:
                setattr(plugin, key, value)
                update_fields.append(key)
        plugin.save(update_fields=update_fields)
        return plugin

    @staticmethod
    def delete(plugin_id: uuid.UUID) -> int:
        """Delete a plugin by ID. Returns number of rows deleted."""
        count, _ = Plugin.objects.filter(id=plugin_id).delete()
        return count


# ---------------------------------------------------------------------------
# Component repositories
# ---------------------------------------------------------------------------


class PluginSkillRepository:
    """Data access for PluginSkill records."""

    @staticmethod
    def list_for_plugin(plugin_id: uuid.UUID) -> QuerySet[PluginSkill]:
        return PluginSkill.objects.filter(plugin_id=plugin_id).order_by(
            "position", "name"
        )

    @staticmethod
    def list_for_plugins(plugin_ids: list[uuid.UUID]) -> QuerySet[PluginSkill]:
        return PluginSkill.objects.filter(plugin_id__in=plugin_ids).order_by(
            "plugin_id", "position", "name"
        )

    @staticmethod
    def list_usable(
        org_id: uuid.UUID, user_id: int, *, activated_ids: set[uuid.UUID]
    ) -> QuerySet[PluginSkill]:
        """Skills of the user's personal plugins and of released shared plugins."""
        shared = Q(
            plugin__owner__isnull=True,
            plugin_id__in=activated_ids,
            plugin__enabled=True,
            plugin__published=True,
        ) & (Q(plugin__organization__isnull=True) | Q(plugin__organization_id=org_id))
        return (
            PluginSkill.objects.filter(Q(plugin__owner_id=user_id) | shared)
            .select_related("plugin")
            .order_by("plugin__name", "position", "name")
        )

    @staticmethod
    def replace_for_plugin(plugin, skills: list[dict]) -> list[PluginSkill]:
        """Replace the skill list of a plugin, keeping the IDs of kept skills.

        Entries match a stored skill by ``id`` (survives renames), then by
        slug. Stable IDs keep the skill selections of chats and scheduled
        tasks valid across edits.
        """
        return _replace_components(
            PluginSkill,
            plugin,
            skills,
            fields=("name", "slug", "description", "body", "position"),
        )


class PluginMcpServerRepository:
    """Data access for PluginMcpServer records."""

    @staticmethod
    def list_for_plugin(plugin_id: uuid.UUID) -> QuerySet[PluginMcpServer]:
        return PluginMcpServer.objects.filter(plugin_id=plugin_id).order_by("name")

    @staticmethod
    def list_oauth_for_plugins(
        plugin_ids: list[uuid.UUID],
    ) -> QuerySet[PluginMcpServer]:
        """Load only OAuth binding fields needed for workspace readiness."""
        return (
            PluginMcpServer.objects.filter(plugin_id__in=plugin_ids, auth_type="oauth")
            .only("plugin_id", "oauth_requirement_key", "url")
            .order_by("plugin_id", "name")
        )

    @staticmethod
    def list_for_plugins(
        plugin_ids: list[uuid.UUID],
    ) -> QuerySet[PluginMcpServer]:
        return PluginMcpServer.objects.filter(plugin_id__in=plugin_ids).order_by(
            "plugin_id", "name"
        )

    @staticmethod
    def list_existing(plugin_id: uuid.UUID) -> list[PluginMcpServer]:
        """Stored servers of one plugin, for matching replacement entries."""
        return list(PluginMcpServer.objects.filter(plugin_id=plugin_id))

    @staticmethod
    def replace_for_plugin(plugin, servers: list[dict]) -> list[PluginMcpServer]:
        """Replace MCP servers, retaining IDs (by ``id``, then slug)."""
        return _replace_components(
            PluginMcpServer,
            plugin,
            servers,
            fields=(
                "tool_policy",
                "runtime_artifact",
                "name",
                "slug",
                "transport",
                "command",
                "args",
                "cwd",
                "env",
                "resources",
                "url",
                "headers",
                "auth_type",
                "oauth_requirement_key",
                "startup_timeout_seconds",
                "request_timeout_seconds",
            ),
        )


class PluginCredentialRequirementRepository:
    """Data access for PluginCredentialRequirement records."""

    @staticmethod
    def list_for_plugin(
        plugin_id: uuid.UUID,
    ) -> QuerySet[PluginCredentialRequirement]:
        return PluginCredentialRequirement.objects.filter(
            plugin_id=plugin_id
        ).select_related("credential_service", "credential", "credential__service")

    @staticmethod
    def list_for_plugins(
        plugin_ids: list[uuid.UUID],
    ) -> QuerySet[PluginCredentialRequirement]:
        return PluginCredentialRequirement.objects.filter(
            plugin_id__in=plugin_ids
        ).select_related("credential_service", "credential", "credential__service")

    @staticmethod
    def delete_for_plugin(plugin_id: uuid.UUID) -> int:
        count, _ = PluginCredentialRequirement.objects.filter(
            plugin_id=plugin_id
        ).delete()
        return count

    @staticmethod
    def bulk_create_for_plugin(plugin, prepared: list[dict]) -> None:
        """Persist prepared requirements for a plugin."""
        instances = [
            PluginCredentialRequirement(
                plugin=plugin,
                key=item["key"],
                credential_service=item["service"],
                credential=item.get("credential"),
                required=item["required"],
                description=item["description"],
            )
            for item in prepared
        ]
        if instances:
            PluginCredentialRequirement.objects.bulk_create(instances)


# ---------------------------------------------------------------------------
# Activation repositories
# ---------------------------------------------------------------------------


class OrgPluginActivationRepository:
    """Data access for OrgPluginActivation records."""

    @staticmethod
    def enabled_plugin_ids(
        org_id: uuid.UUID, *, plugin_ids: list[uuid.UUID] | None = None
    ) -> set[uuid.UUID]:
        """Return org activations, optionally restricted to requested plugins."""
        queryset = OrgPluginActivation.objects.filter(organization_id=org_id)
        if plugin_ids is not None:
            queryset = queryset.filter(plugin_id__in=plugin_ids)
        return set(queryset.values_list("plugin_id", flat=True))

    @staticmethod
    def set_enabled(*, org_id: uuid.UUID, plugin, enabled_by, active: bool) -> bool:
        """Create or delete the org activation row. Returns new state."""
        if active:
            OrgPluginActivation.objects.get_or_create(
                organization_id=org_id,
                plugin=plugin,
                defaults={"enabled_by": enabled_by},
            )
            return True
        OrgPluginActivation.objects.filter(
            organization_id=org_id, plugin_id=plugin.id
        ).delete()
        return False


class WorkspacePluginActivationRepository:
    """Data access for WorkspacePluginActivation records."""

    @staticmethod
    def list_plugin_ids(workspace_id: uuid.UUID) -> list[uuid.UUID]:
        """Return persisted selections in activation order, newest first."""
        return WorkspacePluginActivationRepository.list_plugin_ids_many(
            [workspace_id]
        )[workspace_id]

    @staticmethod
    def list_plugin_ids_many(
        workspace_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """Read selections for authorized workspaces in activation order."""
        grouped: dict[uuid.UUID, list[uuid.UUID]] = {
            workspace_id: [] for workspace_id in workspace_ids
        }
        if not grouped:
            return grouped
        rows = (
            WorkspacePluginActivation.objects.filter(
                workspace_id__in=grouped
            ).values_list("workspace_id", "plugin_id")
        )
        for workspace_id, plugin_id in rows:
            grouped[workspace_id].append(plugin_id)
        return grouped

    @staticmethod
    def enabled_plugin_ids(workspace_id: uuid.UUID) -> set[uuid.UUID]:
        return set(WorkspacePluginActivationRepository.list_plugin_ids(workspace_id))

    @staticmethod
    def selection_fingerprint(workspace_id: uuid.UUID) -> str:
        """Selected plugin IDs with their last change, as one string.

        Plugin edits touch ``Plugin.updated_at``; enabling, disabling and
        organization releases change the selection or its activation rows.
        """
        rows = (
            WorkspacePluginActivation.objects.filter(workspace_id=workspace_id)
            .order_by("plugin_id")
            .values_list("plugin_id", "plugin__updated_at")
        )
        released = OrgPluginActivation.objects.filter(
            plugin__workspace_activations__workspace_id=workspace_id
        ).values_list("organization_id", "plugin_id")
        return "|".join(
            [f"{pid}@{changed.isoformat() if changed else ''}" for pid, changed in rows]
            + sorted(f"{org}:{pid}" for org, pid in released)
        )

    @staticmethod
    def replace_for_workspace(
        workspace_id: uuid.UUID, plugin_ids: list[uuid.UUID], *, enabled_by
    ) -> None:
        """Replace activations in the caller's validated transaction scope."""
        WorkspacePluginActivation.objects.filter(workspace_id=workspace_id).delete()
        if plugin_ids:
            WorkspacePluginActivation.objects.bulk_create(
                [
                    WorkspacePluginActivation(
                        workspace_id=workspace_id,
                        plugin_id=plugin_id,
                        enabled_by=enabled_by,
                    )
                    for plugin_id in plugin_ids
                ],
                ignore_conflicts=True,
            )
