"""Atomic workspace credential/plugin configuration operations."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from apps.credentials.services import CredentialSvc
from apps.plugins.services import PluginSelectionError, PluginService
from apps.runners.enums import TaskType
from common.exceptions import ConflictError, NotFoundError
from common.utils import generate_uuid

from ..repositories import TaskRepository, WorkspaceRepository
from ..workspace_configuration_repository import WorkspaceConfigurationRepository

if TYPE_CHECKING:
    from ..models import Task, Workspace


class WorkspaceConfigurationService:
    """Validate final selections and persist workspace configuration atomically."""

    def __init__(self) -> None:
        self.workspaces = WorkspaceRepository
        self.tasks = TaskRepository
        self.repository = WorkspaceConfigurationRepository
        self.plugins = PluginService()

    def create(
        self,
        *,
        workspace_fields: dict,
        runner: Any,
        user: Any,
        organization_id: uuid.UUID,
        credentials: list,
        plugin_ids: list[uuid.UUID],
        task_id: uuid.UUID,
        operation_payload: dict | None = None,
        credentials_present: bool = False,
        task_type: TaskType = TaskType.CREATE_WORKSPACE,
    ) -> tuple[Workspace, Task]:
        """Create workspace, selections, task, and initial state atomically."""
        selected = workspace_fields.get("base_image_instance")
        with self.repository.creating(
            runner.id, selected.id if selected is not None else None
        ) as selected:
            if selected is not None:
                if selected.captured_image_id and (
                    user is None or selected.created_by_id != user.id
                ):
                    raise NotFoundError("Image artifact", str(selected.id))
                workspace_fields["base_image_instance"] = selected
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
            if (
                credentials_present
                and not credentials
                and operation_payload is not None
            ):
                operation_payload = {
                    **operation_payload,
                    "_unreproducible_credentials": True,
                }
            return self.repository.initialize(
                workspace_fields=workspace_fields,
                runner=runner,
                user=user,
                credentials=credentials,
                plugin_ids=plugin_ids,
                task_id=task_id,
                task_type=task_type,
                operation_payload=operation_payload,
                credentials_present=credentials_present,
            )

    def update(
        self,
        *,
        workspace_id: uuid.UUID,
        user: Any,
        organization_id: uuid.UUID,
        credentials: list | None,
        plugin_ids: list[uuid.UUID] | None,
        name: str | None = None,
        qemu_values: tuple | None = None,
        desktop_values: tuple | None = None,
    ) -> Workspace:
        """Validate and replace final associations/name under a workspace lock."""
        with self.repository.updating(workspace_id) as workspace:
            if (
                workspace.created_by_id != user.id
                or workspace.runner.organization_id != organization_id
            ):
                raise NotFoundError("Workspace", str(workspace_id))
            # The capture parent keeps its hold between child tasks, when
            # current_task_id has already been cleared. Recheck both under lock.
            if workspace.active_operation or workspace.current_task_id:
                raise ConflictError("Workspace lifecycle outcome unresolved")
            current_credentials = self.workspaces.list_attached_credentials(
                workspace.id
            )
            final_credentials = self._resolve_final_credentials(
                current_credentials if credentials is None else credentials,
                owner=workspace.created_by,
                org_id=organization_id,
            )
            current_plugins = list(self.plugins.workspace_plugin_ids(workspace.id))
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
            if qemu_values is not None and workspace.status == "running":
                workspace._lifecycle_task = self.tasks.create(
                    task_id=generate_uuid(),
                    runner=workspace.runner,
                    task_type=TaskType.UPDATE_WORKSPACE,
                    workspace=workspace,
                )
            return workspace

    def credential_deletion_blockers(self, credential: Any) -> list[dict]:
        """Return effective plugin requirements that still need *credential*.

        Covers every workspace with the credential attached, each evaluated
        against its own organization's plugin activations.
        """
        gaps = []
        for workspace_id in self.workspaces.list_ids_for_credential(credential.id):
            workspace = self.workspaces.get_by_id(workspace_id)
            if workspace is None or workspace.runner.organization_id is None:
                continue
            gaps.extend(
                self.plugins.credential_removal_gaps(
                    credential,
                    workspace_id=workspace.id,
                    org_id=workspace.runner.organization_id,
                )
            )
        return gaps

    @staticmethod
    def _resolve_final_credentials(
        credentials: list, *, owner: Any, org_id: uuid.UUID
    ) -> list:
        """Revalidate owner, service boundaries, OAuth bindings and uniqueness."""
        return (
            CredentialSvc()
            .resolve_credentials(
                [credential.id for credential in credentials],
                user=owner,
                org_id=org_id,
            )
            .credentials
        )
