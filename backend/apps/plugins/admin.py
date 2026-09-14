"""Django admin configuration for the plugins app.

Global plugins are editable by staff here; org-owned plugins are managed
by org admins through the REST API.
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


class PluginSkillInline(admin.TabularInline):
    model = PluginSkill
    extra = 0
    fields = ["name", "slug", "position", "body"]


class PluginMcpServerInline(admin.TabularInline):
    model = PluginMcpServer
    extra = 0
    fields = ["name", "slug", "transport", "command", "url"]


class PluginCredentialRequirementInline(admin.TabularInline):
    model = PluginCredentialRequirement
    extra = 0
    fields = [
        "key",
        "credential_service",
        "required",
        "plugin_owned_service",
        "description",
    ]
    readonly_fields = ["plugin_owned_service"]


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
