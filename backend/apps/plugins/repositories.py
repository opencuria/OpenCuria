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


class PluginRepository:
    """Data access for Plugin records."""

    @staticmethod
    def list_visible_to_org(org_id: uuid.UUID) -> QuerySet[Plugin]:
        """Return global + org-owned plugins visible in the org."""
        return (
            Plugin.objects.filter(
                Q(organization__isnull=True) | Q(organization_id=org_id)
            )
            .select_related("organization", "created_by")
            .order_by("name")
        )

    @staticmethod
    def get_visible_by_id(plugin_id: uuid.UUID, org_id: uuid.UUID) -> Plugin | None:
        """Fetch a plugin visible in the org (global or org-owned)."""
        return (
            Plugin.objects.filter(id=plugin_id)
            .filter(Q(organization__isnull=True) | Q(organization_id=org_id))
            .select_related("organization", "created_by")
            .first()
        )

    @staticmethod
    def get_by_id(plugin_id: uuid.UUID) -> Plugin | None:
        """Fetch a plugin by ID."""
        return (
            Plugin.objects.filter(id=plugin_id)
            .select_related("organization", "created_by")
            .first()
        )

    @staticmethod
    def get_global_by_slug(slug: str) -> Plugin | None:
        """Fetch a global plugin by slug."""
        return Plugin.objects.filter(slug=slug, organization__isnull=True).first()

    @staticmethod
    def get_org_by_slug(slug: str, org_id: uuid.UUID) -> Plugin | None:
        """Fetch an org-owned plugin of this org by slug."""
        return Plugin.objects.filter(slug=slug, organization_id=org_id).first()

    @staticmethod
    def list_org_owned(org_id: uuid.UUID) -> QuerySet[Plugin]:
        """Return org-owned plugins of the given org."""
        return Plugin.objects.filter(organization_id=org_id)

    @staticmethod
    def create(
        *,
        name: str,
        slug: str,
        description: str,
        organization_id: uuid.UUID | None,
        created_by,
        enabled: bool = True,
        published: bool = True,
    ) -> Plugin:
        """Create a plugin record."""
        return Plugin.objects.create(
            name=name,
            slug=slug,
            description=description,
            organization_id=organization_id,
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
    def replace_for_plugin(plugin, skills: list[dict]) -> list[PluginSkill]:
        """Atomically replace the skill list of a plugin."""
        PluginSkill.objects.filter(plugin=plugin).delete()
        instances = [
            PluginSkill(
                plugin=plugin,
                name=item["name"],
                slug=item["slug"],
                body=item["body"],
                position=item.get("position", index),
            )
            for index, item in enumerate(skills)
        ]
        if instances:
            PluginSkill.objects.bulk_create(instances)
        return list(PluginSkillRepository.list_for_plugin(plugin.id))

    @staticmethod
    def delete_for_plugin(plugin_id: uuid.UUID) -> int:
        count, _ = PluginSkill.objects.filter(plugin_id=plugin_id).delete()
        return count


class PluginMcpServerRepository:
    """Data access for PluginMcpServer records."""

    @staticmethod
    def list_for_plugin(plugin_id: uuid.UUID) -> QuerySet[PluginMcpServer]:
        return PluginMcpServer.objects.filter(plugin_id=plugin_id).order_by("name")

    @staticmethod
    def list_for_plugins(
        plugin_ids: list[uuid.UUID],
    ) -> QuerySet[PluginMcpServer]:
        return PluginMcpServer.objects.filter(plugin_id__in=plugin_ids).order_by(
            "plugin_id", "name"
        )

    @staticmethod
    def replace_for_plugin(plugin, servers: list[dict]) -> list[PluginMcpServer]:
        """Atomically replace the MCP server list of a plugin."""
        PluginMcpServer.objects.filter(plugin=plugin).delete()
        instances = [PluginMcpServer(plugin=plugin, **item) for item in servers]
        if instances:
            PluginMcpServer.objects.bulk_create(instances)
        return list(PluginMcpServerRepository.list_for_plugin(plugin.id))

    @staticmethod
    def delete_for_plugin(plugin_id: uuid.UUID) -> int:
        count, _ = PluginMcpServer.objects.filter(plugin_id=plugin_id).delete()
        return count


class PluginCredentialRequirementRepository:
    """Data access for PluginCredentialRequirement records."""

    @staticmethod
    def list_for_plugin(
        plugin_id: uuid.UUID,
    ) -> QuerySet[PluginCredentialRequirement]:
        return PluginCredentialRequirement.objects.filter(
            plugin_id=plugin_id
        ).select_related("credential_service")

    @staticmethod
    def list_for_plugins(
        plugin_ids: list[uuid.UUID],
    ) -> QuerySet[PluginCredentialRequirement]:
        return PluginCredentialRequirement.objects.filter(
            plugin_id__in=plugin_ids
        ).select_related("credential_service")

    @staticmethod
    def list_required_service_ids_for_plugins(
        plugin_ids: list[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Return IDs of required services for the given plugins."""
        return set(
            PluginCredentialRequirement.objects.filter(
                plugin_id__in=plugin_ids, required=True
            ).values_list("credential_service_id", flat=True)
        )

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
                required=item["required"],
                description=item["description"],
                plugin_owned_service=item["plugin_owned_service"],
            )
            for item in prepared
        ]
        if instances:
            PluginCredentialRequirement.objects.bulk_create(instances)

    @staticmethod
    def requirements_exist_for_plugins(
        plugin_ids: set[uuid.UUID], service_id: uuid.UUID
    ) -> bool:
        """Return True when any of *plugin_ids* requires *service_id*."""
        if not plugin_ids:
            return False
        return PluginCredentialRequirement.objects.filter(
            plugin_id__in=plugin_ids,
            credential_service_id=service_id,
        ).exists()

    @staticmethod
    def service_ids_used_by_other_org_plugins(
        *, org_id: uuid.UUID, excluded_plugin_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Service IDs still required by other org plugins."""
        other_ids = list(
            Plugin.objects.filter(organization_id=org_id)
            .exclude(id=excluded_plugin_id)
            .values_list("id", flat=True)
        )
        if not other_ids:
            return set()
        return set(
            PluginCredentialRequirement.objects.filter(
                plugin_id__in=other_ids
            ).values_list("credential_service_id", flat=True)
        )


# ---------------------------------------------------------------------------
# Activation repositories
# ---------------------------------------------------------------------------


class OrgPluginActivationRepository:
    """Data access for OrgPluginActivation records."""

    @staticmethod
    def is_enabled(org_id: uuid.UUID, plugin_id: uuid.UUID) -> bool:
        return OrgPluginActivation.objects.filter(
            organization_id=org_id, plugin_id=plugin_id
        ).exists()

    @staticmethod
    def enabled_plugin_ids(org_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            OrgPluginActivation.objects.filter(organization_id=org_id).values_list(
                "plugin_id", flat=True
            )
        )

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
    def enabled_plugin_ids(workspace_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            WorkspacePluginActivation.objects.filter(
                workspace_id=workspace_id
            ).values_list("plugin_id", flat=True)
        )

    @staticmethod
    def list_for_workspace(
        workspace_id: uuid.UUID,
    ) -> QuerySet[WorkspacePluginActivation]:
        return WorkspacePluginActivation.objects.filter(
            workspace_id=workspace_id
        ).select_related("plugin")

    @staticmethod
    def list_for_workspaces(
        workspace_ids: list[uuid.UUID],
    ) -> QuerySet[WorkspacePluginActivation]:
        return WorkspacePluginActivation.objects.filter(
            workspace_id__in=workspace_ids
        ).select_related("plugin")

    @staticmethod
    def replace_for_workspace(
        workspace, plugin_ids: list[uuid.UUID], *, enabled_by
    ) -> None:
        """Atomically replace workspace activations (caller validates)."""
        WorkspacePluginActivation.objects.filter(workspace=workspace).delete()
        if plugin_ids:
            WorkspacePluginActivation.objects.bulk_create(
                [
                    WorkspacePluginActivation(
                        workspace=workspace,
                        plugin_id=plugin_id,
                        enabled_by=enabled_by,
                    )
                    for plugin_id in plugin_ids
                ],
                ignore_conflicts=True,
            )

    @staticmethod
    def workspaces_with_plugin(plugin_id: uuid.UUID) -> QuerySet:
        """Return workspace IDs that have the plugin activated."""
        return WorkspacePluginActivation.objects.filter(
            plugin_id=plugin_id
        ).values_list("workspace_id", flat=True)
