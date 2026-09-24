"""Workspace lifecycle (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces``,
``self.tasks``, ``self.image_instances``, ``self.processes`` plus sibling
helpers (``_dispatch_workspace_task`` / ``_set_workspace_operation`` via
``TaskDispatchMixin``, ``_ensure_*`` guards via ``OwnershipMixin``,
``_forward_*`` via ``FrontendBusMixin``, ``_resolved_credentials_payload``
via :class:`CredentialSyncMixin`, ``mark_processes_killed`` via
:class:`ProcessManagerMixin`). ``CredentialSvc`` is only ever imported
lazily inside method bodies (no new top-level credentials coupling);
the ``update`` / ``resume`` resolve paths prefer the injected
``credential_resolver`` port via ``CredentialSyncMixin``
(see :meth:`_credential_resolve_call`) and keep ``CredentialSvc()`` as
fallback. Phase 4 stream note: ``handle_stream_reply`` prefers an
injected ``harness_stream_router`` port (``route_stream_output(data)`` /
``route_stream_closed(data)``) when the facade was constructed with one;
otherwise the lazy harness path applies. ``fail_streams_for_runner`` and
``ProcessManagerMixin.mark_processes_killed`` keep the lazy
``_ACCESSORS_BY_STREAM`` access as documented remainder.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError, NotFoundError
from common.utils import generate_uuid

from ...desktop import (
    DEFAULT_DESKTOP_HEIGHT,
    DEFAULT_DESKTOP_WIDTH,
    validate_desktop_geometry,
)
from ...enums import (
    RuntimeType,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from ...exceptions import (
    RunnerOfflineError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime cycles
    from apps.credentials.services import ResolvedCredentials

    from ...models import Runner, Task, Workspace

logger = logging.getLogger(__name__)


class WorkspaceLifecycleMixin:
    """Workspace CRUD and lifecycle dispatch shared by RunnerService."""

    async def touch_workspace_activity(
        self,
        workspace_id: uuid.UUID,
        *,
        at: datetime | None = None,
    ) -> None:
        """Update the workspace last-activity timestamp."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            return
        await sync_to_async(self.workspaces.touch_activity)(workspace, at=at)

    @staticmethod
    def _derive_workspace_name(
        name: str, repos: list[str], workspace_id: uuid.UUID
    ) -> str:
        """Return a non-empty workspace name from user input or sensible defaults."""
        trimmed = name.strip()
        if trimmed:
            return trimmed

        if repos:
            last_segment = repos[0].rstrip("/").split("/")[-1]
            cleaned = re.sub(r"\.git$", "", last_segment)
            if cleaned:
                return cleaned

        return f"workspace-{str(workspace_id)[:8]}"

    @staticmethod
    def _validate_runner_qemu_limits(runner: "Runner") -> None:
        """Validate min/max/default and total limits for a runner's QEMU config."""
        if runner.qemu_min_vcpus > runner.qemu_max_vcpus:
            raise ConflictError("Runner vCPU minimum cannot exceed maximum")
        if not (
            runner.qemu_min_vcpus <= runner.qemu_default_vcpus <= runner.qemu_max_vcpus
        ):
            raise ConflictError("Runner default vCPU must be within min/max range")

        if runner.qemu_min_memory_mb > runner.qemu_max_memory_mb:
            raise ConflictError("Runner RAM minimum cannot exceed maximum")
        if not (
            runner.qemu_min_memory_mb
            <= runner.qemu_default_memory_mb
            <= runner.qemu_max_memory_mb
        ):
            raise ConflictError("Runner default RAM must be within min/max range")

        if runner.qemu_min_disk_size_gb > runner.qemu_max_disk_size_gb:
            raise ConflictError("Runner disk minimum cannot exceed maximum")
        if not (
            runner.qemu_min_disk_size_gb
            <= runner.qemu_default_disk_size_gb
            <= runner.qemu_max_disk_size_gb
        ):
            raise ConflictError("Runner default disk must be within min/max range")

        if (
            runner.qemu_max_active_vcpus is not None
            and runner.qemu_max_active_vcpus < runner.qemu_default_vcpus
        ):
            raise ConflictError(
                "Runner total active vCPU limit cannot be smaller than default vCPU"
            )
        if (
            runner.qemu_max_active_memory_mb is not None
            and runner.qemu_max_active_memory_mb < runner.qemu_default_memory_mb
        ):
            raise ConflictError(
                "Runner total active RAM limit cannot be smaller than default RAM"
            )
        if (
            runner.qemu_max_active_disk_size_gb is not None
            and runner.qemu_max_active_disk_size_gb < runner.qemu_default_disk_size_gb
        ):
            raise ConflictError(
                "Runner total active disk limit cannot be smaller than default disk"
            )

    @staticmethod
    def _task_workspace_operation(task_type: TaskType) -> WorkspaceOperation | None:
        """Map a task type to a generic blocking workspace operation."""
        return {
            TaskType.CREATE_WORKSPACE: WorkspaceOperation.CREATING,
            TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT: WorkspaceOperation.CREATING,
            TaskType.UPDATE_WORKSPACE: WorkspaceOperation.RESTARTING,
            TaskType.STOP_WORKSPACE: WorkspaceOperation.STOPPING,
            TaskType.RESUME_WORKSPACE: WorkspaceOperation.STARTING,
            TaskType.REMOVE_WORKSPACE: WorkspaceOperation.REMOVING,
            TaskType.CREATE_IMAGE_ARTIFACT: WorkspaceOperation.CAPTURING_IMAGE,
        }.get(task_type)

    @staticmethod
    def _workspace_operation_label(operation: str | None) -> str:
        """Return a readable operation label for conflict messages."""
        if not operation:
            return "busy"
        return operation.replace("_", " ")

    def _placement_for_image_instance(
        self,
        image: "ImageInstance",
        *,
        organization_id: uuid.UUID | None = None,
        requested_runner_id: uuid.UUID | None = None,
    ) -> tuple["Runner", str]:
        """Resolve runner and runtime from the image, not origin_workspace.

        ``origin_workspace`` is provenance only. Captured images stay
        usable after the source workspace is deleted or pending deletion.
        """
        selected_build_job = image.build_job
        if selected_build_job is not None:
            if selected_build_job.status != "active":
                raise ConflictError("Selected image artifact is not active on runner")
            origin_definition = selected_build_job.image_definition
            if origin_definition is not None and (
                not origin_definition.is_active
                or origin_definition.status != origin_definition.Status.ACTIVE
            ):
                raise ConflictError(
                    "Selected image definition is not available for new workspaces"
                )
            runner = selected_build_job.runner
            runtime_type = (
                origin_definition.runtime_type
                if origin_definition is not None
                else image.runtime_type
            )
        else:
            runner = image.runner
            runtime_type = image.runtime_type

        if runner is None:
            raise ConflictError(
                f"Image artifact '{image.id}' is missing runner placement"
            )
        if organization_id and runner.organization_id != organization_id:
            raise NotFoundError("ImageArtifact", str(image.id))
        if requested_runner_id is not None and requested_runner_id != runner.id:
            raise ConflictError(
                "Selected runner does not have the selected image artifact"
            )
        return runner, runtime_type

    async def create_workspace(
        self,
        *,
        name: str,
        repos: list[str],
        runtime_type: str = "docker",
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        desktop_width: int | None = None,
        desktop_height: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list | None = None,
        ssh_keys: list[str] | None = None,
        credentials: list | None = None,
        runner_id: uuid.UUID | None = None,
        image_artifact_id: uuid.UUID | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["Workspace", "Task"]:
        """
        Create a new workspace on a runner.

        If runner_id is not specified, any online runner in the
        organization is selected.

        Returns the created Workspace and Task records.
        """
        if image_artifact_id is None:
            raise ConflictError("An image artifact is required")

        selected_image = await sync_to_async(self.image_instances.get_by_id)(
            image_artifact_id
        )
        if selected_image is None:
            raise NotFoundError("ImageArtifact", str(image_artifact_id))

        if selected_image.status != "ready":
            raise ConflictError(f"Image artifact '{image_artifact_id}' is not ready")

        runner, runtime_type = self._placement_for_image_instance(
            selected_image,
            organization_id=organization_id,
            requested_runner_id=runner_id,
        )
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=runtime_type,
        )

        self._validate_runner_qemu_limits(runner)
        resolved_qemu_vcpus: int | None = None
        resolved_qemu_memory_mb: int | None = None
        resolved_qemu_disk_size_gb: int | None = None
        if runtime_type == RuntimeType.QEMU:
            (
                resolved_qemu_vcpus,
                resolved_qemu_memory_mb,
                resolved_qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=resolved_qemu_vcpus,
                requested_memory_mb=resolved_qemu_memory_mb,
                requested_disk_size_gb=resolved_qemu_disk_size_gb,
            )

        resolved_desktop_width, resolved_desktop_height = validate_desktop_geometry(
            DEFAULT_DESKTOP_WIDTH if desktop_width is None else desktop_width,
            DEFAULT_DESKTOP_HEIGHT if desktop_height is None else desktop_height,
        )

        # Create records
        workspace_id = generate_uuid()
        workspace_name = self._derive_workspace_name(name, repos, workspace_id)
        workspace = await sync_to_async(self.workspaces.create)(
            workspace_id=workspace_id,
            runner=runner,
            name=workspace_name,
            runtime_type=runtime_type,
            qemu_vcpus=resolved_qemu_vcpus,
            qemu_memory_mb=resolved_qemu_memory_mb,
            qemu_disk_size_gb=resolved_qemu_disk_size_gb,
            desktop_width=resolved_desktop_width,
            desktop_height=resolved_desktop_height,
            base_image_instance=selected_image,
            created_by=user,
        )
        if credentials is not None:
            await sync_to_async(self.workspaces.set_credentials)(workspace, credentials)

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_WORKSPACE,
            workspace=workspace,
        )

        # Dispatch to runner — include workspace_id so the runner
        # uses the same UUID the backend assigned.
        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.CREATE_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "repos": repos,
                "runtime_type": runtime_type,
                "qemu_vcpus": resolved_qemu_vcpus,
                "qemu_memory_mb": resolved_qemu_memory_mb,
                "qemu_disk_size_gb": resolved_qemu_disk_size_gb,
                "configure_commands": [],
                "env_vars": env_vars or {},
                "files": [
                    {
                        "target_path": file.target_path,
                        "content": file.content,
                        "mode": file.mode,
                    }
                    for file in (files or [])
                ],
                "ssh_keys": ssh_keys or [],
                "image_artifact_id": str(image_artifact_id),
                "image_tag": selected_image.runner_ref
                if runtime_type == RuntimeType.DOCKER
                else "",
                "base_image_path": selected_image.runner_ref
                if runtime_type == RuntimeType.QEMU
                else "",
            },
        )
        logger.info(
            "Dispatched create_workspace to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        return workspace, task

    async def update_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        name: str | None = None,
        credentials: list | None = None,
        resolved_credentials: ResolvedCredentials | None = None,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        desktop_width: int | None = None,
        desktop_height: int | None = None,
    ) -> "Workspace":
        """Update mutable workspace metadata and attached credentials."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if name is not None:
            trimmed = name.strip()
            if not trimmed:
                raise ValueError("Workspace name must not be empty")
            workspace = await sync_to_async(self.workspaces.update_name)(
                workspace, trimmed
            )

        credential_records = (
            resolved_credentials.credentials
            if resolved_credentials is not None
            else credentials
        )
        if credential_records is not None:
            current_ids = {
                credential.id for credential in workspace.credentials.all()
            }
            new_ids = {credential.id for credential in credential_records}
            ids_changed = current_ids != new_ids
            desired_present = bool(new_ids)
            needs_disk_sync = workspace.status == WorkspaceStatus.RUNNING and (
                ids_changed
                or bool(workspace.credentials_present) != desired_present
            )
            if needs_disk_sync:
                runner = workspace.runner
                if not runner.is_online:
                    raise RunnerOfflineError(str(runner.id))

            if ids_changed:
                workspace = await sync_to_async(self.workspaces.set_credentials)(
                    workspace,
                    credential_records,
                )

            if needs_disk_sync:
                resolved = resolved_credentials
                if resolved is None:
                    # Phase-4 port: prefer the injected credential_resolver.
                    resolve_call = self._credential_resolve_call(workspace)
                    resolved = await sync_to_async(resolve_call)(workspace)
                await self._dispatch_credential_inject(
                    workspace,
                    resolved=resolved,
                    wait=True,
                )

        qemu_fields_requested = any(
            value is not None
            for value in (qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb)
        )
        if qemu_fields_requested:
            if workspace.runtime_type != RuntimeType.QEMU:
                raise ValueError("QEMU resources can only be set for QEMU workspaces")
            if workspace.status not in (
                WorkspaceStatus.RUNNING,
                WorkspaceStatus.STOPPED,
            ):
                raise WorkspaceStateError(
                    f"Workspace '{workspace_id}' is '{workspace.status}', must be running or stopped to reconfigure resources"
                )

            runner = workspace.runner
            self._validate_runner_qemu_limits(runner)
            current = (
                workspace.qemu_vcpus or runner.qemu_default_vcpus,
                workspace.qemu_memory_mb or runner.qemu_default_memory_mb,
                workspace.qemu_disk_size_gb or runner.qemu_default_disk_size_gb,
            )
            (
                resolved_qemu_vcpus,
                resolved_qemu_memory_mb,
                resolved_qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                current=current,
            )
            qemu_resources_changed = current != (
                resolved_qemu_vcpus,
                resolved_qemu_memory_mb,
                resolved_qemu_disk_size_gb,
            )
            if qemu_resources_changed:
                await self._ensure_qemu_active_capacity(
                    runner=runner,
                    requested_vcpus=resolved_qemu_vcpus,
                    requested_memory_mb=resolved_qemu_memory_mb,
                    requested_disk_size_gb=resolved_qemu_disk_size_gb,
                    exclude_workspace_id=workspace.id,
                )

                workspace = await sync_to_async(self.workspaces.update_qemu_resources)(
                    workspace,
                    qemu_vcpus=resolved_qemu_vcpus,
                    qemu_memory_mb=resolved_qemu_memory_mb,
                    qemu_disk_size_gb=resolved_qemu_disk_size_gb,
                )

                if workspace.status == WorkspaceStatus.RUNNING:
                    runner = workspace.runner
                    if not runner.is_online:
                        raise RunnerOfflineError(str(runner.id))
                    # Reconfigure restarts the VM: fail workspace streams
                    # so harness waiters surface it instead of hanging.
                    await sync_to_async(self.mark_processes_killed)(
                        str(workspace_id), reason="workspace_reconfigured"
                    )

                    task_id = generate_uuid()
                    task = await sync_to_async(self.tasks.create)(
                        task_id=task_id,
                        runner=runner,
                        task_type=TaskType.UPDATE_WORKSPACE,
                        workspace=workspace,
                    )
                    await self._dispatch_workspace_task(
                        runner=runner,
                        event="task:update_workspace",
                        task=task,
                        workspace=workspace,
                        operation=self._task_workspace_operation(
                            TaskType.UPDATE_WORKSPACE
                        ),
                        payload={
                            "task_id": str(task_id),
                            "workspace_id": str(workspace_id),
                            "qemu_vcpus": resolved_qemu_vcpus,
                            "qemu_memory_mb": resolved_qemu_memory_mb,
                            "qemu_disk_size_gb": resolved_qemu_disk_size_gb,
                        },
                    )

        if desktop_width is not None or desktop_height is not None:
            resolved_desktop_width, resolved_desktop_height = validate_desktop_geometry(
                workspace.desktop_width if desktop_width is None else desktop_width,
                workspace.desktop_height if desktop_height is None else desktop_height,
            )
            if (
                resolved_desktop_width != workspace.desktop_width
                or resolved_desktop_height != workspace.desktop_height
            ):
                workspace = await sync_to_async(
                    self.workspaces.update_desktop_geometry
                )(
                    workspace,
                    desktop_width=resolved_desktop_width,
                    desktop_height=resolved_desktop_height,
                )

        return await sync_to_async(self.workspaces.get_by_id)(workspace_id)

    async def rename_workspace(self, workspace_id: uuid.UUID, name: str) -> "Workspace":
        """Rename an existing workspace."""
        return await self.update_workspace(workspace_id, name=name)

    async def stop_workspace(self, workspace_id: uuid.UUID) -> "Task":
        """Stop a running workspace."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to stop"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.STOP_WORKSPACE,
            workspace=workspace,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:stop_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.STOP_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )
        return task

    async def resume_workspace(self, workspace_id: uuid.UUID) -> "Task":
        """Resume a stopped workspace."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.STOPPED:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.STOPPED}' to resume"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=workspace.runtime_type,
        )

        qemu_vcpus = workspace.qemu_vcpus
        qemu_memory_mb = workspace.qemu_memory_mb
        qemu_disk_size_gb = workspace.qemu_disk_size_gb
        if workspace.runtime_type == RuntimeType.QEMU:
            self._validate_runner_qemu_limits(runner)
            (
                qemu_vcpus,
                qemu_memory_mb,
                qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=qemu_vcpus,
                requested_memory_mb=qemu_memory_mb,
                requested_disk_size_gb=qemu_disk_size_gb,
                exclude_workspace_id=workspace.id,
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.RESUME_WORKSPACE,
            workspace=workspace,
        )

        # Phase-4 port: prefer the injected credential_resolver.
        resolve_call = self._credential_resolve_call(workspace)
        workspace_credentials = await sync_to_async(resolve_call)(workspace)

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:resume_workspace",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.RESUME_WORKSPACE),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "qemu_vcpus": qemu_vcpus,
                "qemu_memory_mb": qemu_memory_mb,
                "qemu_disk_size_gb": qemu_disk_size_gb,
                **self._resolved_credentials_payload(workspace_credentials),
            },
        )
        return task

    async def remove_workspace(self, workspace_id: uuid.UUID) -> "Task":
        """Remove a workspace and its container.

        If the runner is online, dispatches the delete command immediately and
        sets status to ``deleting``.  If the runner is offline, sets status to
        ``pending_deletion`` — the job will be delivered on reconnect.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        if workspace.status in (
            WorkspaceStatus.PENDING_DELETION,
            WorkspaceStatus.DELETING,
            WorkspaceStatus.DELETED,
        ):
            raise ConflictError(
                f"Workspace '{workspace.id}' is already in deletion state '{workspace.status}'"
            )

        if (
            workspace.active_operation
            and workspace.active_operation != WorkspaceOperation.REMOVING
        ):
            raise ConflictError(
                f"Workspace '{workspace.id}' is currently {self._workspace_operation_label(workspace.active_operation)}"
            )

        runner = workspace.runner
        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.REMOVE_WORKSPACE,
            workspace=workspace,
        )

        if runner.is_online:
            previous_status = workspace.status
            await sync_to_async(self.workspaces.mark_deleting)(workspace_id)
            try:
                await self._dispatch_workspace_task(
                    runner=runner,
                    event="task:remove_workspace",
                    task=task,
                    workspace=workspace,
                    operation=self._task_workspace_operation(TaskType.REMOVE_WORKSPACE),
                    payload={
                        "task_id": str(task_id),
                        "workspace_id": str(workspace_id),
                    },
                )
            except Exception:
                await sync_to_async(self.workspaces.update_status)(
                    workspace,
                    previous_status,
                )
                raise
        else:
            await sync_to_async(self.workspaces.mark_pending_deletion)(workspace_id)

        self._forward_to_frontend(
            "workspace:status_changed",
            {
                "workspace_id": str(workspace_id),
                "status": WorkspaceStatus.DELETING
                if runner.is_online
                else WorkspaceStatus.PENDING_DELETION,
                "task_id": str(task_id),
            },
            str(workspace_id),
        )

        return task

    def handle_workspace_created(
        self,
        task_id: str,
        workspace_id: str,
        status: str,
        runner_id: str | None = None,
        credentials_present: bool | None = None,
    ) -> None:
        """Handle workspace:created event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:created for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace is None:
            logger.warning("Task %s has no associated workspace", task_id)
            return

        self.workspaces.update_status(workspace, WorkspaceStatus.RUNNING)
        self.workspaces.update_active_operation(workspace, None)
        if credentials_present is not None:
            self.workspaces.update_credentials_present(
                workspace, bool(credentials_present)
            )
        self.tasks.complete(task)
        logger.info("Workspace created: %s", workspace_id)

        self._forward_workspace_status(workspace, task_id=task_id)
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_stopped(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:stopped event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:stopped for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_status(workspace, WorkspaceStatus.STOPPED)
            self.workspaces.update_active_operation(workspace, None)
            self.workspaces.update_credentials_present(workspace, False)
            self._pending_credential_inject.discard(
                (str(workspace.runner_id), str(workspace.id))
            )
            self.mark_processes_killed(str(workspace.id), reason="workspace_stopped")
        self._cleanup_desktop_state(workspace_id)
        self.tasks.complete(task)
        logger.info("Workspace stopped: %s", workspace_id)

        if workspace:
            self._forward_workspace_status(workspace, task_id=task_id)
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_resumed(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        credentials_present: bool | None = None,
    ) -> None:
        """Handle workspace:resumed event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:resumed for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_status(workspace, WorkspaceStatus.RUNNING)
            self.workspaces.update_active_operation(workspace, None)
            if credentials_present is not None:
                self.workspaces.update_credentials_present(
                    workspace, bool(credentials_present)
                )
        self.tasks.complete(task)
        logger.info("Workspace resumed: %s", workspace_id)

        if workspace:
            self._forward_workspace_status(workspace, task_id=task_id)
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_updated(
        self, task_id: str, workspace_id: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:updated event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:updated for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_active_operation(workspace, None)
        self.tasks.complete(task)
        logger.info("Workspace updated: %s", workspace_id)
        self._forward_workspace_operation(workspace_id, None)

    def handle_workspace_error(
        self, task_id: str, error: str, runner_id: str | None = None
    ) -> None:
        """Handle workspace:error event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:error for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        # If this was a workspace lifecycle creation task, mark workspace failed.
        workspace = task.workspace
        workspace_id = str(workspace.id) if workspace else None
        if workspace:
            self.workspaces.update_active_operation(workspace, None)
            self._pending_credential_inject.discard(
                (str(workspace.runner_id), str(workspace.id))
            )
        if workspace and task.type in {
            TaskType.CREATE_WORKSPACE,
            TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
        }:
            self.workspaces.update_status(workspace, WorkspaceStatus.FAILED)
        elif workspace and task.type == TaskType.REMOVE_WORKSPACE:
            self.workspaces.mark_delete_failed(workspace.id, error=error)

        self.tasks.fail(task, error)
        logger.error("Workspace error (task=%s): %s", task_id, error)

        if workspace_id:
            if workspace and task.type == TaskType.REMOVE_WORKSPACE:
                self._forward_to_frontend(
                    "workspace:status_changed",
                    {
                        "workspace_id": workspace_id,
                        "status": WorkspaceStatus.DELETE_FAILED,
                        "task_id": task_id,
                    },
                    workspace_id,
                )
            self._forward_workspace_operation(workspace_id, None)
            self._forward_to_frontend(
                "workspace:error",
                {"workspace_id": workspace_id, "task_id": task_id, "error": error},
                workspace_id,
            )

    def handle_workspace_removed(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        result: str = "deleted",
        already_absent: bool = False,
    ) -> None:
        """Handle workspace:removed event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning("Received workspace:removed for unknown task: %s", task_id)
            return

        if not self._validate_task_runner(task, runner_id):
            return

        delete_result = result or ("already_absent" if already_absent else "deleted")
        if delete_result not in {"deleted", "already_absent"}:
            error = f"Workspace delete was not confirmed: {delete_result}"
            if task.workspace:
                self.workspaces.mark_delete_failed(task.workspace.id, error=error)
            self.tasks.fail(task, error)
            logger.warning(
                "Workspace delete failed confirmation: %s (%s)",
                workspace_id,
                delete_result,
            )
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.mark_deleted(workspace.id)
            self.mark_processes_killed(str(workspace.id), reason="workspace_removed")
        self._cleanup_desktop_state(workspace_id)

        self.tasks.complete(task)
        logger.info(
            "Workspace removed: %s (result=%s already_absent=%s)",
            workspace_id,
            delete_result,
            already_absent,
        )

        self._forward_to_frontend(
            "workspace:status_changed",
            {
                "workspace_id": workspace_id,
                "status": "deleted",
                "task_id": task_id,
            },
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    async def dispatch_pending_workspace_deletions(self, runner: "Runner") -> list:
        """Dispatch pending workspace deletions that accumulated while runner was offline."""
        from ...models import Workspace

        pending = await sync_to_async(
            lambda: list(
                Workspace.objects.filter(
                    runner=runner,
                    status__in=[
                        WorkspaceStatus.PENDING_DELETION,
                        WorkspaceStatus.DELETING,
                    ],
                )
            )
        )()

        dispatched = []
        for ws in pending:
            try:
                # Find existing task
                from ...models import Task as TaskModel

                task = await sync_to_async(
                    lambda: TaskModel.objects.filter(
                        workspace=ws,
                        type=TaskType.REMOVE_WORKSPACE,
                        status__in=[TaskStatus.PENDING, TaskStatus.IN_PROGRESS],
                    ).first()
                )()
                reused_active_task = (
                    task is not None and ws.status == WorkspaceStatus.DELETING
                )
                if task is None:
                    task_id = generate_uuid()
                    task = await sync_to_async(self.tasks.create)(
                        task_id=task_id,
                        runner=runner,
                        task_type=TaskType.REMOVE_WORKSPACE,
                        workspace=ws,
                    )
                if not reused_active_task:
                    await sync_to_async(self.workspaces.mark_deleting)(ws.id)
                await self._emit_to_runner(
                    runner,
                    "task:remove_workspace",
                    {
                        "task_id": str(task.id),
                        "workspace_id": str(ws.id),
                    },
                )
                await sync_to_async(self.tasks.mark_in_progress)(task)
                dispatched.append(ws)
            except Exception:
                logger.exception(
                    "Failed to dispatch pending workspace deletion %s for runner %s",
                    ws.id,
                    runner.id,
                )
        return dispatched

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: uuid.UUID,
        name: str = "",
        env_vars: dict[str, str] | None = None,
        files: list | None = None,
        ssh_keys: list[str] | None = None,
        credentials: list | None = None,
        user=None,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["Workspace", "Task"]:
        """Create a workspace from an image artifact.

        Credentials are explicitly supplied by the caller and persisted in
        the new workspace until a controlled stop. Captured artifacts do
        not retain credential associations.
        """
        from apps.credentials.services import CredentialSvc

        # Phase-4 port: the uniqueness check needs the full CredentialSvc
        # API; an injected credential_resolver only replaces
        # resolve_workspace_credentials, so keep the concrete service here.
        # (getattr fallback: a fake resolver without the uniqueness method
        # would break this path — always use CredentialSvc for the check.)
        credential_svc = CredentialSvc()
        image = await sync_to_async(self.image_instances.get_by_id)(image_artifact_id)
        if image is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' not found")

        if image.status != "ready":
            raise ConflictError(f"Image artifact '{image_artifact_id}' is not ready")

        runner, runtime_type = self._placement_for_image_instance(
            image,
            organization_id=organization_id,
        )
        source_workspace = image.origin_workspace
        qemu_vcpus = getattr(source_workspace, "qemu_vcpus", None)
        qemu_memory_mb = getattr(source_workspace, "qemu_memory_mb", None)
        qemu_disk_size_gb = getattr(source_workspace, "qemu_disk_size_gb", None)
        desktop_width = (
            source_workspace.desktop_width
            if source_workspace is not None
            else DEFAULT_DESKTOP_WIDTH
        )
        desktop_height = (
            source_workspace.desktop_height
            if source_workspace is not None
            else DEFAULT_DESKTOP_HEIGHT
        )

        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=runtime_type,
        )
        if runtime_type == RuntimeType.QEMU:
            self._validate_runner_qemu_limits(runner)
            (
                qemu_vcpus,
                qemu_memory_mb,
                qemu_disk_size_gb,
            ) = self._resolve_qemu_resources(
                runner=runner,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
            )
            await self._ensure_qemu_active_capacity(
                runner=runner,
                requested_vcpus=qemu_vcpus,
                requested_memory_mb=qemu_memory_mb,
                requested_disk_size_gb=qemu_disk_size_gb,
            )

        resolved_env_vars = env_vars or {}
        resolved_files = files or []
        resolved_ssh_keys = ssh_keys or []

        if credentials is not None:
            await sync_to_async(credential_svc.assert_unique_workspace_credentials)(
                credentials
            )

        workspace_id = generate_uuid()
        workspace_name = self._derive_workspace_name(name, [], workspace_id)
        if not name:
            workspace_name = f"{workspace_name} (clone)"

        workspace = await sync_to_async(self.workspaces.create)(
            workspace_id=workspace_id,
            runner=runner,
            name=workspace_name,
            runtime_type=runtime_type,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            desktop_width=desktop_width,
            desktop_height=desktop_height,
            base_image_instance=image,
            created_by=user,
        )

        if credentials is not None:
            await sync_to_async(self.workspaces.set_credentials)(workspace, credentials)

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
            workspace=workspace,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_workspace_from_image_artifact",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(
                TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT
            ),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "image_artifact_id": image.runner_ref,
                "runtime_type": runtime_type,
                "qemu_vcpus": qemu_vcpus,
                "qemu_memory_mb": qemu_memory_mb,
                "qemu_disk_size_gb": qemu_disk_size_gb,
                "env_vars": resolved_env_vars,
                "files": [
                    {
                        "target_path": file.target_path,
                        "content": file.content,
                        "mode": file.mode,
                    }
                    for file in resolved_files
                ],
                "ssh_keys": resolved_ssh_keys,
            },
        )
        logger.info(
            "Dispatched create_workspace_from_image_artifact (workspace=%s, artifact=%s, task=%s)",
            workspace_id,
            image_artifact_id,
            task_id,
        )
        return workspace, task
