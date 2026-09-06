from __future__ import annotations

from django.contrib import admin

from .models import (
    ImageDefinition,
    ImageInstance,
    Runner,
    ImageBuildJob,
    RunnerSystemMetrics,
    Task,
    Workspace,
    WorkspaceProcess,
)


@admin.register(Runner)
class RunnerAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "status", "available_runtimes", "connected_at"]
    list_filter = ["status"]
    readonly_fields = ["id", "api_token_hash", "created_at", "updated_at"]


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "runner", "status", "runtime_type", "created_at"]
    list_filter = ["status", "runtime_type"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ["id", "runner", "type", "status", "created_at", "completed_at"]
    list_filter = ["type", "status"]
    readonly_fields = ["id", "created_at"]
@admin.register(RunnerSystemMetrics)
class RunnerSystemMetricsAdmin(admin.ModelAdmin):
    list_display = ["runner", "timestamp", "cpu_usage_percent", "ram_used_bytes", "disk_used_bytes"]
    list_filter = ["runner", "timestamp"]
    search_fields = ["runner__name"]
    readonly_fields = [
        "runner",
        "timestamp",
        "cpu_usage_percent",
        "ram_used_bytes",
        "ram_total_bytes",
        "disk_used_bytes",
        "disk_total_bytes",
        "vm_metrics",
    ]


@admin.register(ImageInstance)
class ImageInstanceAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "origin_workspace",
        "origin_definition",
        "runner",
        "name",
        "runner_ref",
        "origin_type",
        "status",
        "size_bytes",
        "created_at",
    ]
    readonly_fields = ["id", "created_at", "updated_at", "deleted_at"]


@admin.register(ImageDefinition)
class ImageDefinitionAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "runtime_type", "base_distro", "organization", "is_active", "updated_at"]
    list_filter = ["runtime_type", "is_active", "organization"]
    search_fields = ["name", "description", "base_distro"]


@admin.register(ImageBuildJob)
class ImageBuildJobAdmin(admin.ModelAdmin):
    list_display = ["id", "image_definition", "runner", "status", "built_at", "updated_at"]
    list_filter = ["status", "runner", "image_definition"]
    search_fields = ["image_definition__name", "runner__name", "image_instance__runner_ref"]


@admin.register(WorkspaceProcess)
class WorkspaceProcessAdmin(admin.ModelAdmin):
    list_display = ["id", "workspace", "name", "status", "pid", "exit_code", "started_at"]
    list_filter = ["status"]
    readonly_fields = ["id", "started_at", "ended_at", "updated_at"]
