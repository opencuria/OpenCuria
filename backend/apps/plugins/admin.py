"""Django admin configuration for the plugins app.

Global plugins are editable by staff here; org-owned plugins are managed
by org admins through the REST API. The built-in OpenCuria plugin and its
MCP server cannot be deleted.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    PluginSkill,
    WorkspacePluginActivation,
)
from .services import OPENCURIA_PLUGIN_ID


class PluginSkillInline(admin.TabularInline):
    model = PluginSkill
    extra = 0
    fields = ["name", "slug", "position", "body"]


class PluginMcpServerInline(admin.TabularInline):
    model = PluginMcpServer
    extra = 0
    fields = ["name", "slug", "transport", "command", "url"]

    def has_delete_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.id == OPENCURIA_PLUGIN_ID:
            return False
        return super().has_delete_permission(request, obj)


class PluginCredentialRequirementInline(admin.TabularInline):
    model = PluginCredentialRequirement
    extra = 0
    fields = ["key", "credential_service", "required", "description"]


@admin.register(Plugin)
class PluginAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "slug",
        "organization",
        "enabled",
        "published",
        "created_at",
    ]
    list_filter = ["enabled", "published", "organization"]
    search_fields = ["name", "slug", "organization__name"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [
        PluginSkillInline,
        PluginMcpServerInline,
        PluginCredentialRequirementInline,
    ]

    def has_delete_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.id == OPENCURIA_PLUGIN_ID:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset) -> None:
        for plugin in queryset:
            if self.has_delete_permission(request, plugin):
                plugin.delete()


@admin.register(PluginSkill)
class PluginSkillAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "plugin", "position"]
    list_filter = ["plugin"]
    search_fields = ["name", "slug"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(PluginMcpServer)
class PluginMcpServerAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "plugin", "transport"]
    list_filter = ["transport", "plugin"]
    search_fields = ["name", "slug"]
    readonly_fields = ["id", "created_at", "updated_at"]

    def has_delete_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.plugin_id == OPENCURIA_PLUGIN_ID:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset) -> None:
        for server in queryset:
            if self.has_delete_permission(request, server):
                server.delete()


@admin.register(PluginCredentialRequirement)
class PluginCredentialRequirementAdmin(admin.ModelAdmin):
    list_display = ["key", "plugin", "credential_service", "required"]
    list_filter = ["required", "plugin"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(OrgPluginActivation)
class OrgPluginActivationAdmin(admin.ModelAdmin):
    list_display = ["id", "organization", "plugin", "created_at"]
    list_filter = ["organization", "plugin"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WorkspacePluginActivation)
class WorkspacePluginActivationAdmin(admin.ModelAdmin):
    list_display = ["id", "workspace", "plugin", "created_at"]
    list_filter = ["plugin"]
    readonly_fields = ["id", "created_at", "updated_at"]
