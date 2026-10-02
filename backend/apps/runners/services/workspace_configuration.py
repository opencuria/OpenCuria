"""Atomic workspace credential/plugin configuration operations."""

from __future__ import annotations

import uuid

from django.db import transaction

from apps.plugins.services import PluginSelectionError, PluginService
from apps.runners.enums import TaskType
from common.exceptions import NotFoundError

from ..models import Task, Workspace
from ..repositories import TaskRepository, WorkspaceRepository


class WorkspaceConfigurationService:
    """Validate final selections and persist workspace configuration atomically."""

    def __init__(self) -> None:
        self.workspaces = WorkspaceRepository
        self.tasks = TaskRepository
        self.plugins = PluginService()

    def create(
        self,
        *,
        workspace_fields: dict,
        runner,
        user,
        organization_id: uuid.UUID,
        credentials: list,
        plugin_ids: list[uuid.UUID],
        task_id: uuid.UUID,
        credentials_present: bool = False,
        task_type: TaskType = TaskType.CREATE_WORKSPACE,
    ) -> tuple[Workspace, Task]:
        """Create workspace, selections, task, and initial state atomically."""
        with transaction.atomic():
            credentials = self._resolve_final_credentials(
                credentials, owner=user, org_id=organization_id
            )
            gaps = self.plugins.validate_workspace_plugin_selection(
                plugin_ids=plugin_ids,
                credentials=credentials,
                owner=user,
                org_id=organization_id,
            )
            if gaps:
                raise PluginSelectionError(
                    "Missing required credentials for selected plugins", gaps
                )
            workspace = self.workspaces.create(**workspace_fields)
            self.workspaces.set_credentials(workspace, credentials)
            if credentials_present:
                self.workspaces.update_credentials_present(workspace, True)
                workspace = self.workspaces.get_by_id(workspace.id)
            self.plugins.workspace_activations.replace_for_workspace(
                workspace, list(dict.fromkeys(plugin_ids)), enabled_by=user
            )
            task = self.tasks.create(
                task_id=task_id,
                runner=runner,
                task_type=task_type,
                workspace=workspace,
            )
            return self.workspaces.get_by_id(workspace.id), task

    def update(
        self,
        *,
        workspace_id: uuid.UUID,
        user,
        organization_id: uuid.UUID,
        credentials: list | None,
        plugin_ids: list[uuid.UUID] | None,
        name: str | None = None,
        qemu_values: tuple | None = None,
        desktop_values: tuple | None = None,
    ) -> Workspace:
        """Validate and replace final associations/name under a workspace lock."""
        with transaction.atomic():
            workspace = self.workspaces.get_by_id(workspace_id, lock=True)
            if workspace is None:
                raise NotFoundError("Workspace", str(workspace_id))
            if (
                workspace.created_by_id != user.id
                or workspace.runner.organization_id != organization_id
            ):
                raise NotFoundError("Workspace", str(workspace_id))
            current_credentials = self.workspaces.list_attached_credentials(
                workspace.id
            )
            final_credentials = self._resolve_final_credentials(
                current_credentials if credentials is None else credentials,
                owner=workspace.created_by,
                org_id=organization_id,
            )
            current_plugins = list(
                self.plugins.workspace_activations.enabled_plugin_ids(workspace.id)
            )
            final_plugins = current_plugins if plugin_ids is None else plugin_ids
            gaps = self.plugins.validate_workspace_plugin_selection(
                plugin_ids=final_plugins,
                credentials=final_credentials,
                owner=workspace.created_by,
                org_id=organization_id,
                allow_unavailable=(plugin_ids is None),
            )
            if gaps:
                raise PluginSelectionError(
                    "Missing required credentials for selected plugins", gaps
                )
            workspace = self.workspaces.replace_configuration(
                workspace,
                name=name,
                credentials=(final_credentials if credentials is not None else None),
                plugin_ids=plugin_ids,
                enabled_by=user,
                qemu_values=qemu_values,
                desktop_values=desktop_values,
            )
            return workspace

    @staticmethod
    def _resolve_final_credentials(
        credentials: list, *, owner, org_id: uuid.UUID
    ) -> list:
        """Revalidate owner, service boundaries, OAuth bindings and uniqueness."""
        from apps.credentials.services import CredentialSvc

        return (
            CredentialSvc()
            .resolve_credentials(
                [credential.id for credential in credentials],
                user=owner,
                org_id=org_id,
            )
            .credentials
        )
