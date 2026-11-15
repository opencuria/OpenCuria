"""Compose workspace read metadata independently of REST and other adapters.

Repositories load related records; credential and plugin services own the
visible selections. This service owns derived presence, auto-stop and image
metadata and returns plain values for adapter response validation.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from apps.credentials.services import CredentialSvc
from apps.plugins.services import PluginService

from ..disk_usage import WorkspaceDiskUsage
from ..enums import RunnerStatus, WorkspaceStatus
from ..models import Workspace
from ..workspace_read_repository import WorkspaceReadRelations, WorkspaceReadRepository
from .image_versions import ImageVersionService


class WorkspaceReadService:
    """Build workspace details from persisted state and domain read services."""

    def __init__(
        self,
        *,
        repository: type[WorkspaceReadRepository] | None = None,
        credentials: CredentialSvc | None = None,
        plugins: PluginService | None = None,
        versions: ImageVersionService | None = None,
    ) -> None:
        """Inject read boundaries without constructing runner execution."""
        self.repository = repository or WorkspaceReadRepository
        self.credentials = credentials or CredentialSvc()
        self.plugins = plugins or PluginService()
        self.versions = versions or ImageVersionService()

    @staticmethod
    def _summary_values(
        related: WorkspaceReadRelations, *, prefer_annotation: bool
    ) -> dict[str, Any]:
        """Share presence and image naming while preserving annotation semantics."""
        runner_online = related.runner_status == RunnerStatus.ONLINE
        if prefer_annotation and related.annotated_runner_online is not None:
            runner_online = related.annotated_runner_online
        base_image_name = related.base_image_definition_name or (
            related.base_image.name if related.base_image is not None else None
        )
        return {
            "runner_online": runner_online,
            "base_image_name": base_image_name or None,
        }

    def summary(self, workspace: Workspace) -> dict[str, Any]:
        """Return live presence and image naming for compact discovery views."""
        related = self.repository.load_relations(workspace, include_pending_image=False)
        return self._summary_values(related, prefer_annotation=False)

    def describe(self, workspace: Workspace) -> dict[str, Any]:
        """Describe an already-authorized workspace without exposing secrets."""
        return self.describe_many([workspace])[0]

    def describe_many(self, workspaces: list[Workspace]) -> list[dict[str, Any]]:
        """Describe authorized workspaces with bounded metadata query counts."""
        if not workspaces:
            return []
        workspace_ids = [workspace.id for workspace in workspaces]
        related = self.repository.load_relations_many(workspaces)
        credentials = self.credentials.workspace_credential_ids_many(workspace_ids)
        plugins = self.plugins.list_workspace_plugin_ids_many(workspace_ids)
        versions = self.versions.version_refs(
            [
                image
                for facts in related.values()
                for image in (facts.base_image, facts.pending_base_image)
                if image is not None
            ]
        )
        return [
            self._describe(
                workspace,
                related[workspace.id],
                credential_ids=credentials[workspace.id],
                plugin_ids=plugins[workspace.id],
                versions=versions,
            )
            for workspace in workspaces
        ]

    def _describe(
        self,
        workspace: Workspace,
        related: WorkspaceReadRelations,
        *,
        credential_ids: list[uuid.UUID],
        plugin_ids: list[uuid.UUID],
        versions: dict[uuid.UUID, dict],
    ) -> dict[str, Any]:
        """Compose one workspace from request-local metadata without I/O."""
        timeout = related.auto_stop_timeout_minutes
        auto_stop_at = None
        if (
            timeout
            and workspace.status == WorkspaceStatus.RUNNING
            and workspace.last_activity_at is not None
        ):
            auto_stop_at = workspace.last_activity_at + timedelta(minutes=timeout)
        disk_usage = WorkspaceDiskUsage.from_workspace(workspace)
        return {
            "id": workspace.id,
            "runner_id": workspace.runner_id,
            "status": workspace.status,
            "active_operation": workspace.active_operation,
            "intervention_required": workspace.intervention_required,
            "lifecycle_diagnostic": workspace.lifecycle_diagnostic,
            "name": workspace.name,
            "qemu_vcpus": workspace.qemu_vcpus,
            "qemu_memory_mb": workspace.qemu_memory_mb,
            "qemu_disk_size_gb": workspace.qemu_disk_size_gb,
            "disk_usage": asdict(disk_usage) if disk_usage else None,
            "desktop_width": workspace.desktop_width,
            "desktop_height": workspace.desktop_height,
            "created_by_id": workspace.created_by_id,
            "last_activity_at": workspace.last_activity_at,
            "auto_stop_timeout_minutes": timeout,
            "auto_stop_at": auto_stop_at,
            "delete_requested_at": getattr(workspace, "delete_requested_at", None),
            "delete_started_at": getattr(workspace, "delete_started_at", None),
            "delete_confirmed_at": getattr(workspace, "delete_confirmed_at", None),
            "delete_last_error": getattr(workspace, "delete_last_error", "") or "",
            "delete_attempt_count": getattr(workspace, "delete_attempt_count", 0) or 0,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "has_active_session": bool(
                getattr(workspace, "has_active_harness_session", False)
            ),
            **self._summary_values(related, prefer_annotation=True),
            "credential_ids": credential_ids,
            "plugin_ids": plugin_ids,
            "credentials_present": bool(
                getattr(workspace, "credentials_present", False)
            ),
            "base_image": (
                versions[related.base_image.id] if related.base_image else None
            ),
            "pending_base_image": (
                versions[related.pending_base_image.id]
                if related.pending_base_image
                else None
            ),
            "repos": list(getattr(workspace, "repos", None) or []),
        }
