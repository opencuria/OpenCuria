"""
Service layer for the runners app.

All business logic lives here. API views and WebSocket consumers delegate
to these functions — they never contain business logic themselves.

The service layer uses repositories for data access and the Socket.IO
server instance for sending events to runners.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from asgiref.sync import async_to_sync, sync_to_async
from django.utils import timezone
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from apps.credentials.services import CredentialSvc
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import generate_uuid, hash_token, verify_token

from .enums import (
    RunnerStatus,
    RuntimeType,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from .exceptions import (
    NoAvailableRunnerError,
    RunnerNotFoundError,
    RunnerOfflineError,
    TaskNotFoundError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)
from .repositories import (
    ImageDefinitionRepository,
    ImageInstanceRepository,
    RunnerRepository,
    ImageBuildJobRepository,
    TaskRepository,
    WorkspaceRepository,
)

logger = logging.getLogger(__name__)


class RunnerService:
    """
    Central business logic for runner management and task dispatching.

    This is the only place where domain rules are enforced. All interfaces
    (REST API, Socket.IO consumers) delegate to this service.
    """

    def __init__(self, sio_server=None):
        """
        Initialize the service.

        Args:
            sio_server: The python-socketio AsyncServer instance for sending
                        events to connected runners. Injected to keep the
                        service testable.
        """
        self.sio = sio_server
        self.runners = RunnerRepository
        self.workspaces = WorkspaceRepository
        self.tasks = TaskRepository
        self.image_instances = ImageInstanceRepository
        self.image_definitions = ImageDefinitionRepository
        self.build_jobs = ImageBuildJobRepository
        # Tracks unknown runtime workspaces for which a cleanup request has
        # already been sent, to avoid emitting duplicate cleanup tasks on every
        # heartbeat while one is still in flight.
        self._pending_unknown_workspace_cleanup: set[tuple[str, str]] = set()
        self._pending_credential_inject: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Runner lifecycle
    # ------------------------------------------------------------------

    def authenticate_runner(self, token: str) -> "Runner":
        """
        Authenticate a runner by its API token.

        Returns the Runner instance if valid, raises AuthenticationError otherwise.
        """
        token_hash = hash_token(token)
        runner = self.runners.get_by_token_hash(token_hash)
        if runner is None:
            raise AuthenticationError("Invalid runner API token")
        return runner

    def register_runner(
        self,
        runner: "Runner",
        *,
        sid: str,
        available_runtimes: list[str] | None = None,
    ) -> "Runner":
        """
        Mark a runner as online after it connects and sends runner:register.

        Args:
            runner: The authenticated Runner instance.
            sid: Socket.IO session ID for targeted messaging.
            available_runtimes: List of runtime types the runner supports.
        """
        runner = self.runners.set_online(
            runner,
            sid=sid,
            available_runtimes=available_runtimes or ["docker"],
        )
        logger.info(
            "Runner registered: %s",
            runner.id,
        )
        return runner

    async def dispatch_pending_image_builds(self, runner: "Runner") -> list:
        """Dispatch pending image builds that were created while the runner was offline.

        This is called after a runner registers online.  It queries for
        ``ImageBuildJob`` records with status ``pending`` and no associated
        build task, then triggers the regular build pipeline for each.

        Returns the list of dispatched ImageBuildJob records.
        """
        from .models import ImageBuildJob

        await sync_to_async(self.timeout_stale_image_operations)()

        pending_builds = await sync_to_async(
            lambda: list(
                ImageBuildJob.objects.filter(
                    runner=runner,
                    status=ImageBuildJob.Status.PENDING,
                    build_task__isnull=True,
                ).select_related("image_definition", "runner")
            )
        )()

        dispatched = []
        for build in pending_builds:
            try:
                await self.trigger_build_job(
                    image_definition=build.image_definition,
                    runner=runner,
                    activate=True,
                )
                dispatched.append(build)
                logger.info(
                    "Dispatched pending image build %s for runner %s",
                    build.id,
                    runner.id,
                )
            except Exception:
                logger.exception(
                    "Failed to dispatch pending image build %s for runner %s",
                    build.id,
                    runner.id,
                )
        return dispatched

    async def dispatch_pending_image_deletions(self, runner: "Runner") -> list:
        """Dispatch pending image deletions that accumulated while runner was offline."""
        from .models import ImageInstance

        pending_images = await sync_to_async(
            lambda: list(self.image_instances.list_pending_delete_for_runner(runner.id))
        )()

        dispatched = []
        for image in pending_images:
            try:
                reused_active_task = False
                if image.deleting_task_id:
                    existing_task = await sync_to_async(self.tasks.get_by_id)(
                        uuid.UUID(image.deleting_task_id)
                    )
                    if existing_task and existing_task.status in {
                        TaskStatus.PENDING,
                        TaskStatus.IN_PROGRESS,
                    }:
                        task = existing_task
                        reused_active_task = image.status == ImageInstance.Status.DELETING
                    else:
                        task = None
                else:
                    task = None
                if task is None:
                    task_id = generate_uuid()
                    task = await sync_to_async(self.tasks.create)(
                        task_id=task_id,
                        runner=runner,
                        task_type=TaskType.DELETE_IMAGE,
                    )
                if not reused_active_task:
                    await sync_to_async(self.image_instances.mark_deleting)(
                        image.id,
                        deleting_task_id=str(task.id),
                    )
                await self._emit_to_runner(
                    runner,
                    "task:delete_image_artifact",
                    {
                        "task_id": str(task.id),
                        "image_instance_id": str(image.id),
                        "runtime_type": image.runtime_type,
                        "image_artifact_id": image.runner_ref,
                    },
                )
                await sync_to_async(self.tasks.mark_in_progress)(task)
                dispatched.append(image)
            except Exception:
                logger.exception(
                    "Failed to dispatch pending image deletion %s for runner %s",
                    image.id,
                    runner.id,
                )
        return dispatched

    def unregister_runner(self, sid: str) -> None:
        """
        Mark a runner as offline when it disconnects.

        Looks up the runner by its Socket.IO session ID.
        """
        from .models import Runner

        try:
            runner = Runner.objects.get(sid=sid, status=RunnerStatus.ONLINE)
        except Runner.DoesNotExist:
            logger.warning("Disconnect from unknown SID: %s", sid)
            return

        self.runners.set_offline(runner)
        logger.info("Runner unregistered: %s", runner.id)

        # Notify frontend about runner going offline so it can update display.
        self._forward_runner_status_to_frontend(runner, "offline")

    def _forward_runner_status_to_frontend(
        self,
        runner: "Runner",
        status: str,
    ) -> None:
        """Emit runner status change events for all workspaces of this runner.

        Sends a ``runner:offline`` or ``runner:online`` event for every
        workspace managed by the runner so subscribed frontend clients can
        update their display without a full page refresh.
        """
        workspaces = list(self.workspaces.list_by_runner(runner.id).exclude(
            status__in=[WorkspaceStatus.REMOVED, WorkspaceStatus.FAILED]
        ))
        event = "runner:offline" if status == "offline" else "runner:online"
        for ws in workspaces:
            ws_id = str(ws.id)
            self._forward_to_frontend(
                event,
                {"workspace_id": ws_id, "runner_id": str(runner.id)},
                ws_id,
            )

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

    # ------------------------------------------------------------------
    # Workspace operations (initiated by REST API or frontend)
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_workspace_name(name: str, repos: list[str], workspace_id: uuid.UUID) -> str:
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
        if not (runner.qemu_min_vcpus <= runner.qemu_default_vcpus <= runner.qemu_max_vcpus):
            raise ConflictError("Runner default vCPU must be within min/max range")

        if runner.qemu_min_memory_mb > runner.qemu_max_memory_mb:
            raise ConflictError("Runner RAM minimum cannot exceed maximum")
        if not (runner.qemu_min_memory_mb <= runner.qemu_default_memory_mb <= runner.qemu_max_memory_mb):
            raise ConflictError("Runner default RAM must be within min/max range")

        if runner.qemu_min_disk_size_gb > runner.qemu_max_disk_size_gb:
            raise ConflictError("Runner disk minimum cannot exceed maximum")
        if not (runner.qemu_min_disk_size_gb <= runner.qemu_default_disk_size_gb <= runner.qemu_max_disk_size_gb):
            raise ConflictError("Runner default disk must be within min/max range")

        if runner.qemu_max_active_vcpus is not None and runner.qemu_max_active_vcpus < runner.qemu_default_vcpus:
            raise ConflictError("Runner total active vCPU limit cannot be smaller than default vCPU")
        if runner.qemu_max_active_memory_mb is not None and runner.qemu_max_active_memory_mb < runner.qemu_default_memory_mb:
            raise ConflictError("Runner total active RAM limit cannot be smaller than default RAM")
        if runner.qemu_max_active_disk_size_gb is not None and runner.qemu_max_active_disk_size_gb < runner.qemu_default_disk_size_gb:
            raise ConflictError("Runner total active disk limit cannot be smaller than default disk")

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

    def _ensure_workspace_available(self, workspace: "Workspace") -> None:
        """Reject mutating operations while another blocking lifecycle action runs."""
        if workspace.status in (
            WorkspaceStatus.PENDING_DELETION,
            WorkspaceStatus.DELETING,
            WorkspaceStatus.DELETED,
        ):
            raise ConflictError(
                f"Workspace '{workspace.id}' is pending deletion and cannot be modified"
            )
        if workspace.active_operation:
            raise ConflictError(
                f"Workspace '{workspace.id}' is currently {self._workspace_operation_label(workspace.active_operation)}"
            )

    def _forward_workspace_operation(
        self,
        workspace_id: str,
        active_operation: str | None,
    ) -> None:
        """Forward workspace operation changes to subscribed frontend clients."""
        self._forward_to_frontend(
            "workspace:operation_changed",
            {
                "workspace_id": workspace_id,
                "active_operation": active_operation,
            },
            workspace_id,
        )

    def _forward_workspace_status(
        self,
        workspace: "Workspace",
        *,
        task_id: str | None = None,
        status: str | None = None,
    ) -> None:
        """Forward workspace status and credential presence to the frontend."""
        payload = {
            "workspace_id": str(workspace.id),
            "status": status or workspace.status,
            "credentials_present": bool(workspace.credentials_present),
        }
        if task_id:
            payload["task_id"] = task_id
        self._forward_to_frontend(
            "workspace:status_changed",
            payload,
            str(workspace.id),
        )

    @staticmethod
    def _resolved_credentials_payload(resolved) -> dict:
        """Serialize resolved credentials for a runner task payload."""
        return {
            "env_vars": dict(getattr(resolved, "env_vars", None) or {}),
            "files": [
                {
                    "target_path": file.target_path,
                    "content": file.content,
                    "mode": file.mode,
                }
                for file in (getattr(resolved, "files", None) or [])
            ],
            "ssh_keys": list(getattr(resolved, "ssh_keys", None) or []),
        }

    async def _dispatch_credential_inject(self, workspace: "Workspace") -> None:
        """Replace persisted credentials on a running workspace."""
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        key = (str(runner.id), str(workspace.id))
        if key in self._pending_credential_inject:
            return

        resolved = await sync_to_async(
            CredentialSvc().resolve_workspace_credentials
        )(workspace)
        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.INJECT_CREDENTIALS,
            workspace=workspace,
        )
        self._pending_credential_inject.add(key)
        try:
            await self._emit_to_runner(
                runner,
                "task:inject_credentials",
                {
                    "task_id": str(task_id),
                    "workspace_id": str(workspace.id),
                    **self._resolved_credentials_payload(resolved),
                },
            )
            await sync_to_async(self.tasks.mark_in_progress)(task)
        except Exception as exc:
            self._pending_credential_inject.discard(key)
            await sync_to_async(self.tasks.fail)(task, str(exc))
            raise

    async def _set_workspace_operation(
        self,
        workspace: "Workspace",
        operation: WorkspaceOperation | None,
    ) -> "Workspace":
        """Persist and broadcast a workspace operation change."""
        workspace = await sync_to_async(self.workspaces.update_active_operation)(
            workspace,
            operation,
        )
        self._forward_workspace_operation(str(workspace.id), workspace.active_operation)
        return workspace

    async def _dispatch_workspace_task(
        self,
        *,
        runner: "Runner",
        event: str,
        payload: dict,
        task: "Task",
        workspace: "Workspace" | None = None,
        operation: WorkspaceOperation | None = None,
    ) -> None:
        """Set busy state, emit the task to the runner, and roll back on dispatch failure."""
        try:
            if workspace is not None and operation is not None:
                await self._set_workspace_operation(workspace, operation)
            await self._emit_to_runner(runner, event, payload)
            await sync_to_async(self.tasks.mark_in_progress)(task)
        except Exception as exc:
            if workspace is not None and operation is not None:
                await self._set_workspace_operation(workspace, None)
            await sync_to_async(self.tasks.fail)(task, str(exc))
            raise

    @staticmethod
    def _resolve_qemu_resources(
        *,
        runner: "Runner",
        qemu_vcpus: int | None,
        qemu_memory_mb: int | None,
        qemu_disk_size_gb: int | None,
        current: tuple[int, int, int] | None = None,
    ) -> tuple[int, int, int]:
        """Resolve effective QEMU resources and validate against runner limits."""
        current_vcpus = current[0] if current else runner.qemu_default_vcpus
        current_memory_mb = current[1] if current else runner.qemu_default_memory_mb
        current_disk_size_gb = current[2] if current else runner.qemu_default_disk_size_gb

        resolved_vcpus = qemu_vcpus if qemu_vcpus is not None else current_vcpus
        resolved_memory_mb = qemu_memory_mb if qemu_memory_mb is not None else current_memory_mb
        resolved_disk_size_gb = qemu_disk_size_gb if qemu_disk_size_gb is not None else current_disk_size_gb

        if not (runner.qemu_min_vcpus <= resolved_vcpus <= runner.qemu_max_vcpus):
            raise ConflictError(
                f"vCPU value must be between {runner.qemu_min_vcpus} and {runner.qemu_max_vcpus}"
            )
        if not (runner.qemu_min_memory_mb <= resolved_memory_mb <= runner.qemu_max_memory_mb):
            raise ConflictError(
                f"RAM value must be between {runner.qemu_min_memory_mb} and {runner.qemu_max_memory_mb} MiB"
            )
        if not (runner.qemu_min_disk_size_gb <= resolved_disk_size_gb <= runner.qemu_max_disk_size_gb):
            raise ConflictError(
                f"Disk value must be between {runner.qemu_min_disk_size_gb} and {runner.qemu_max_disk_size_gb} GiB"
            )
        return resolved_vcpus, resolved_memory_mb, resolved_disk_size_gb

    @staticmethod
    def _ensure_runner_supports_runtime(
        *,
        runner: "Runner",
        runtime_type: str,
    ) -> None:
        """Raise when a runner does not advertise support for a runtime."""
        if runtime_type not in (runner.available_runtimes or []):
            raise ConflictError(f"Runner does not support runtime '{runtime_type}'")

    async def _ensure_qemu_active_capacity(
        self,
        *,
        runner: "Runner",
        requested_vcpus: int,
        requested_memory_mb: int,
        requested_disk_size_gb: int,
        exclude_workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Ensure active QEMU aggregate limits allow the requested resources."""
        active_qemu = await sync_to_async(list)(
            self.workspaces.list_running_qemu_by_runner(runner.id)
        )

        total_vcpus = 0
        total_memory_mb = 0
        total_disk_size_gb = 0
        for ws in active_qemu:
            if exclude_workspace_id and ws.id == exclude_workspace_id:
                continue
            total_vcpus += ws.qemu_vcpus or runner.qemu_default_vcpus
            total_memory_mb += ws.qemu_memory_mb or runner.qemu_default_memory_mb
            total_disk_size_gb += ws.qemu_disk_size_gb or runner.qemu_default_disk_size_gb

        next_total_vcpus = total_vcpus + requested_vcpus
        next_total_memory_mb = total_memory_mb + requested_memory_mb
        next_total_disk_size_gb = total_disk_size_gb + requested_disk_size_gb

        if (
            runner.qemu_max_active_vcpus is not None
            and next_total_vcpus > runner.qemu_max_active_vcpus
        ):
            raise ConflictError(
                f"Runner active vCPU limit exceeded ({next_total_vcpus}/{runner.qemu_max_active_vcpus})"
            )
        if (
            runner.qemu_max_active_memory_mb is not None
            and next_total_memory_mb > runner.qemu_max_active_memory_mb
        ):
            raise ConflictError(
                f"Runner active RAM limit exceeded ({next_total_memory_mb}/{runner.qemu_max_active_memory_mb} MiB)"
            )
        if (
            runner.qemu_max_active_disk_size_gb is not None
            and next_total_disk_size_gb > runner.qemu_max_active_disk_size_gb
        ):
            raise ConflictError(
                f"Runner active disk limit exceeded ({next_total_disk_size_gb}/{runner.qemu_max_active_disk_size_gb} GiB)"
            )

    async def create_workspace(
        self,
        *,
        name: str,
        repos: list[str],
        runtime_type: str = "docker",
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
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

        selected_build_job = selected_image.build_job
        source_workspace = selected_image.origin_workspace
        requested_runner_id = runner_id
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
            if (
                organization_id
                and selected_build_job.runner.organization_id != organization_id
            ):
                raise NotFoundError("ImageArtifact", str(image_artifact_id))
            if (
                requested_runner_id is not None
                and requested_runner_id != selected_build_job.runner_id
            ):
                raise ConflictError(
                    "Selected runner does not have the selected image artifact"
                )
            runtime_type = selected_build_job.image_definition.runtime_type
            runner_id = selected_build_job.runner_id
        else:
            if source_workspace is None:
                raise ConflictError("Captured image artifact is missing its source workspace")
            if (
                organization_id
                and source_workspace.runner.organization_id != organization_id
            ):
                raise NotFoundError("ImageArtifact", str(image_artifact_id))
            if (
                requested_runner_id is not None
                and requested_runner_id != source_workspace.runner_id
            ):
                raise ConflictError(
                    "Selected runner does not have the selected image artifact"
                )
            runtime_type = source_workspace.runtime_type
            runner_id = source_workspace.runner_id

        # Find a suitable runner
        if runner_id:
            runner = await sync_to_async(self.runners.get_by_id)(runner_id)
            if runner is None:
                raise RunnerNotFoundError(str(runner_id))
            if not runner.is_online:
                raise RunnerOfflineError(str(runner_id))
            # Verify runner belongs to the organization
            if organization_id and runner.organization_id != organization_id:
                raise RunnerNotFoundError(str(runner_id))
        else:
            # Pick any online runner
            runners_qs = self.runners.list_by_organization(organization_id).filter(
                status=RunnerStatus.ONLINE
            ) if organization_id else self.runners.list_online()
            runner = await sync_to_async(lambda: runners_qs.first())()
            if runner is None:
                raise NoAvailableRunnerError("any")

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
                "image_tag": selected_image.runner_ref if runtime_type == RuntimeType.DOCKER else "",
                "base_image_path": selected_image.runner_ref if runtime_type == RuntimeType.QEMU else "",
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
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
    ) -> "Workspace":
        """Update mutable workspace metadata and attached credentials."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        if name is not None:
            trimmed = name.strip()
            if not trimmed:
                raise ValueError("Workspace name must not be empty")
            workspace = await sync_to_async(self.workspaces.update_name)(workspace, trimmed)

        if credentials is not None:
            workspace = await sync_to_async(self.workspaces.set_credentials)(
                workspace,
                credentials,
            )
            if workspace.status == WorkspaceStatus.RUNNING:
                runner = workspace.runner
                if not runner.is_online:
                    raise RunnerOfflineError(str(runner.id))
                await self._dispatch_credential_inject(workspace)

        self._ensure_workspace_available(workspace)

        qemu_fields_requested = any(
            value is not None
            for value in (qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb)
        )
        if qemu_fields_requested:
            if workspace.runtime_type != RuntimeType.QEMU:
                raise ValueError("QEMU resources can only be set for QEMU workspaces")
            if workspace.status not in (WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED):
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
                        operation=self._task_workspace_operation(TaskType.UPDATE_WORKSPACE),
                        payload={
                            "task_id": str(task_id),
                            "workspace_id": str(workspace_id),
                            "qemu_vcpus": resolved_qemu_vcpus,
                            "qemu_memory_mb": resolved_qemu_memory_mb,
                            "qemu_disk_size_gb": resolved_qemu_disk_size_gb,
                        },
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

        workspace_credentials = await sync_to_async(
            CredentialSvc().resolve_workspace_credentials
        )(workspace)

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

        if workspace.active_operation and workspace.active_operation != WorkspaceOperation.REMOVING:
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
                "status": WorkspaceStatus.DELETING if runner.is_online else WorkspaceStatus.PENDING_DELETION,
                "task_id": str(task_id),
            },
            str(workspace_id),
        )

        return task

    # ------------------------------------------------------------------
    # Event handlers (called by Socket.IO consumer when runner reports back)
    # ------------------------------------------------------------------

    def _validate_task_runner(self, task, runner_id: str | None) -> bool:
        """Return True if the task belongs to the given runner.

        When *runner_id* is None the check is skipped (e.g. in tests).
        Logs a warning and returns False on ownership mismatch.
        """
        if runner_id is None:
            return True
        if str(task.runner_id) != runner_id:
            logger.warning(
                "Event rejected: task %s belongs to runner %s, not %s",
                task.id,
                task.runner_id,
                runner_id,
            )
            return False
        return True

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

    def handle_credentials_injected(
        self,
        task_id: str,
        workspace_id: str,
        credentials_present: bool,
        runner_id: str | None = None,
    ) -> None:
        """Handle workspace:credentials_injected event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning(
                "Received workspace:credentials_injected for unknown task: %s",
                task_id,
            )
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if workspace:
            self.workspaces.update_credentials_present(
                workspace, bool(credentials_present)
            )
            self._pending_credential_inject.discard(
                (str(workspace.runner_id), str(workspace.id))
            )
            self._forward_workspace_status(workspace, task_id=task_id)
        self.tasks.complete(task)
        logger.info(
            "Workspace credentials injected: %s present=%s",
            workspace_id,
            credentials_present,
        )

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
            logger.warning(
                "Received workspace:removed for unknown task: %s", task_id
            )
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

    # ------------------------------------------------------------------
    # Terminal (interactive PTY)
    # ------------------------------------------------------------------

    # In-memory mapping: workspace_id (str) → terminal_id (str)
    _active_terminals: dict[str, str] = {}
    # In-memory mapping: workspace_id (str) → runner_id (str)
    # Populated when a terminal starts so terminal:output can be validated
    # without a DB lookup on every single chunk.
    _terminal_workspace_runner: dict[str, str] = {}

    # In-memory desktop session state
    _active_desktops: dict[str, dict] = {}
    _desktop_workspace_runner: dict[str, str] = {}

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> "Task":
        """Dispatch a start_terminal task to the runner.

        Returns the Task record.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a terminal"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_TERMINAL,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:start_terminal",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "cols": cols,
                "rows": rows,
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_terminal to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def handle_terminal_started(
        self,
        task_id: str,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:started event from a runner."""
        from .sio_server import emit_to_frontend

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        self._active_terminals[workspace_id] = terminal_id
        # Cache runner ownership so terminal:output validation is O(1)
        if runner_id:
            self._terminal_workspace_runner[workspace_id] = runner_id
        logger.info(
            "Terminal started: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )

        await emit_to_frontend(
            "terminal:started",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "task_id": task_id,
            },
            workspace_id,
        )

    async def handle_terminal_output(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:output from runner — forward to frontend (no DB).

        Runner ownership is validated against the in-memory cache populated
        when the terminal session was started, so no DB lookup is required
        on the hot path.
        """
        if runner_id:
            cached = self._terminal_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "terminal:output rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        from .sio_server import emit_to_frontend

        await emit_to_frontend(
            "terminal:output",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
            workspace_id,
        )

    async def handle_terminal_closed(
        self,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:closed from runner."""
        if runner_id:
            cached = self._terminal_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "terminal:closed rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        from .sio_server import emit_to_frontend

        self._active_terminals.pop(workspace_id, None)
        self._terminal_workspace_runner.pop(workspace_id, None)
        logger.info(
            "Terminal closed: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )
        await emit_to_frontend(
            "terminal:closed",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
            workspace_id,
        )

    async def forward_terminal_input(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
    ) -> None:
        """Forward terminal input from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:input",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def forward_terminal_resize(
        self,
        workspace_id: str,
        terminal_id: str,
        cols: int,
        rows: int,
    ) -> None:
        """Forward terminal resize from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:resize",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "cols": cols,
                "rows": rows,
            },
        )

    async def forward_terminal_close(
        self,
        workspace_id: str,
        terminal_id: str,
    ) -> None:
        """Forward terminal close request from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:close",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
        )

    # ------------------------------------------------------------------
    # Desktop session (KasmVNC)
    # ------------------------------------------------------------------

    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> "Task":
        """Dispatch a start_desktop task to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a desktop"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_DESKTOP,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:start_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_desktop to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def stop_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> "Task":
        """Dispatch a stop_desktop task to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.STOP_DESKTOP,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:stop_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched stop_desktop to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        return task

    async def write_desktop_clipboard(
        self,
        workspace_id: uuid.UUID,
        text: str,
    ) -> None:
        """Write plain text into a running desktop session clipboard."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        if not self.is_desktop_active(str(workspace_id)):
            raise ConflictError(
                "Desktop session is not active. Start the desktop first."
            )

        response = await self._call_runner(
            runner,
            "desktop:clipboard_write",
            {
                "workspace_id": str(workspace_id),
                "text": text,
            },
            timeout=120,
        )
        if isinstance(response, dict) and response.get("ok") is False:
            raise RuntimeError(str(response.get("error") or "Clipboard write failed"))
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def read_desktop_clipboard(
        self,
        workspace_id: uuid.UUID,
    ) -> str:
        """Read plain text from a running desktop session clipboard."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        if not self.is_desktop_active(str(workspace_id)):
            raise ConflictError(
                "Desktop session is not active. Start the desktop first."
            )

        response = await self._call_runner(
            runner,
            "desktop:clipboard_read",
            {
                "workspace_id": str(workspace_id),
            },
            timeout=120,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        if isinstance(response, dict) and response.get("ok") is False:
            raise RuntimeError(str(response.get("error") or "Clipboard read failed"))
        if not isinstance(response, dict):
            return ""
        value = response.get("text", "")
        return value if isinstance(value, str) else str(value)

    async def handle_desktop_started(
        self,
        task_id: str | None,
        workspace_id: str,
        port: int,
        container_ip: str,
        network_name: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle desktop:started event from a runner."""
        from .sio_server import emit_to_frontend

        task = None
        if task_id:
            task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
            if task and not self._validate_task_runner(task, runner_id):
                return

        desktop_state = {
            "port": port,
            "container_ip": container_ip,
            "network_name": network_name,
        }

        self._record_active_desktop(
            workspace_id,
            desktop_state,
            runner_id=runner_id,
        )

        if task:
            await sync_to_async(self.tasks.complete)(task)

        logger.info(
            "Desktop started: workspace=%s, port=%s, ip=%s",
            workspace_id,
            port,
            container_ip,
        )

        await emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
            },
            workspace_id,
        )

    async def handle_desktop_stopped(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle desktop:stopped event from a runner."""
        from .sio_server import emit_to_frontend

        if runner_id:
            cached = self._desktop_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "desktop:stopped rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        desktop_info = self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)

        logger.info("Desktop stopped: workspace=%s", workspace_id)

        await emit_to_frontend(
            "desktop:stopped",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
            },
            workspace_id,
        )

    def is_desktop_active(self, workspace_id: str) -> bool:
        """Check if a desktop session is active for a workspace."""
        return workspace_id in self._active_desktops

    def get_desktop_info(self, workspace_id: str) -> dict | None:
        """Get cached desktop session info if the runner reported one active."""
        return self._active_desktops.get(workspace_id)

    def _record_active_desktop(
        self,
        workspace_id: str,
        desktop_state: dict,
        *,
        runner_id: str | None = None,
    ) -> None:
        """Persist backend desktop state without performing network I/O."""
        self._active_desktops[workspace_id] = dict(desktop_state)
        if runner_id:
            self._desktop_workspace_runner[workspace_id] = runner_id

    def _sync_desktop_state_from_heartbeat(
        self,
        workspace_id: str,
        desktop_state: dict | None,
        *,
        runner_id: str,
    ) -> None:
        """Reconcile cached desktop state from runner heartbeats."""
        if not desktop_state:
            self._cleanup_desktop_state(workspace_id)
            return

        current = self._active_desktops.get(workspace_id)
        current_runner = self._desktop_workspace_runner.get(workspace_id)
        if current == desktop_state and current_runner == runner_id:
            return

        self._cleanup_desktop_state(workspace_id)
        self._record_active_desktop(
            workspace_id,
            desktop_state,
            runner_id=runner_id,
        )

    def _cleanup_desktop_state(self, workspace_id: str) -> None:
        """Remove in-memory desktop state for a workspace."""
        self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)

    # ------------------------------------------------------------------
    # File explorer (stateless passthrough — no DB models)
    # ------------------------------------------------------------------

    async def forward_files_event(
        self,
        workspace_id: str,
        event: str,
        data: dict,
    ) -> None:
        """Forward a file explorer event from frontend to the runner.

        Looks up the workspace's runner and emits the event directly.
        When the runner is offline, a synthetic error result is sent back
        to the frontend so callers do not get stuck waiting indefinitely.
        """
        from .sio_server import emit_to_frontend

        # Map request event → result event for error responses.
        _result_event: dict[str, str] = {
            "files:read": "files:content_result",
            "files:list": "files:list_result",
            "files:upload": "files:upload_result",
            "files:download": "files:download_result",
        }

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            result_event = _result_event.get(event)
            if result_event:
                error_payload: dict = {
                    "workspace_id": workspace_id,
                    "request_id": data.get("request_id", ""),
                    "path": data.get("path", ""),
                    "error": "Runner is offline",
                }
                if result_event == "files:content_result":
                    error_payload.update({"content": "", "size": 0, "truncated": False})
                elif result_event == "files:list_result":
                    error_payload["entries"] = []
                elif result_event == "files:upload_result":
                    error_payload["status"] = "error"
                await emit_to_frontend(result_event, error_payload, workspace_id)
            return

        await self._emit_to_runner(runner, event, data)
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def handle_files_result(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Forward a file result event from runner to subscribed frontends."""
        routed = self._route_harness_reply(event, data, runner_id=runner_id)
        if routed is not None:
            return
        from .sio_server import emit_to_frontend

        workspace_id = data.get("workspace_id", "")
        if runner_id and workspace_id:
            try:
                workspace = await sync_to_async(self.workspaces.get_by_id)(
                    uuid.UUID(workspace_id)
                )
                if workspace is None or str(workspace.runner_id) != runner_id:
                    logger.warning(
                        "%s rejected: workspace %s does not belong to runner %s",
                        event,
                        workspace_id,
                        runner_id,
                    )
                    return
            except (ValueError, TypeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s", event, workspace_id
                )
                return

        await emit_to_frontend(event, data, workspace_id)

    def _get_workspace_auto_stop_deadline(self, workspace) -> datetime | None:
        """Return the inactivity deadline for a workspace, or None when disabled."""
        organization = getattr(getattr(workspace, "runner", None), "organization", None)
        timeout_minutes = getattr(
            organization,
            "workspace_auto_stop_timeout_minutes",
            None,
        )
        if (
            timeout_minutes is None
            or workspace.status != WorkspaceStatus.RUNNING
            or workspace.last_activity_at is None
        ):
            return None
        return workspace.last_activity_at + timedelta(minutes=timeout_minutes)

    def _should_auto_stop_workspace(
        self,
        workspace,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return True if inactivity policy requires stopping the workspace."""
        if workspace.status != WorkspaceStatus.RUNNING:
            return False
        if workspace.active_operation:
            return False
        runner = getattr(workspace, "runner", None)
        if runner is None or not runner.is_online:
            return False
        deadline = self._get_workspace_auto_stop_deadline(workspace)
        if deadline is None:
            return False
        return deadline <= (now or timezone.now())

    async def auto_stop_inactive_workspaces(
        self,
        *,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> list["Task"]:
        """Stop running workspaces whose inactivity deadline has elapsed."""
        evaluation_time = now or timezone.now()
        if runner_id is not None:
            workspaces = await sync_to_async(
                lambda: list(self.workspaces.list_by_runner(runner_id))
            )()
        elif organization_id is not None:
            workspaces = await sync_to_async(
                lambda: list(self.workspaces.list_by_organization(organization_id))
            )()
        else:
            workspaces = await sync_to_async(lambda: list(self.workspaces.list_all()))()

        dispatched: list["Task"] = []
        for workspace in workspaces:
            if not self._should_auto_stop_workspace(workspace, now=evaluation_time):
                continue
            try:
                task = await self.stop_workspace(workspace.id)
            except (ConflictError, RunnerOfflineError, WorkspaceStateError) as exc:
                logger.info(
                    "Skipped auto-stop for workspace %s: %s",
                    workspace.id,
                    exc,
                )
                continue
            logger.info(
                "Dispatched inactivity auto-stop for workspace %s",
                workspace.id,
            )
            dispatched.append(task)

        return dispatched

    def handle_heartbeat(
        self,
        runner: "Runner",
        workspaces: list[dict],
    ) -> None:
        """Handle runner:heartbeat event — reconcile workspace state.

        Compares the runner's reported container states with the backend's
        records and updates any stale entries.

        Args:
            runner: The Runner that sent the heartbeat.
            workspaces: List of dicts with workspace_id and status.
        """
        from django.utils import timezone as tz

        # Update heartbeat timestamp
        self.runners.update_heartbeat(runner)

        # Build lookup of runner-reported workspace states
        runner_ws_payloads: dict[str, dict] = {}
        runner_ws_states: dict[str, str] = {}
        for ws_data in workspaces:
            ws_id = ws_data.get("workspace_id", "")
            status = ws_data.get("status", "unknown")
            runner_ws_payloads[ws_id] = ws_data
            runner_ws_states[ws_id] = status

        # Check backend workspaces for this runner
        backend_workspaces = list(
            self.workspaces.list_by_runner(runner.id)
        )
        backend_workspace_ids = {str(ws.id) for ws in backend_workspaces}
        runner_id_str = str(runner.id)

        # Drop stale pending cleanup entries that are no longer present on the
        # runner heartbeat.
        active_pending = {
            key
            for key in self._pending_unknown_workspace_cleanup
            if key[0] == runner_id_str
        }
        for key in active_pending:
            if key[1] not in runner_ws_states:
                self._pending_unknown_workspace_cleanup.discard(key)

        # Runner reported instances that backend does not know: request cleanup.
        unknown_workspace_ids = sorted(
            set(runner_ws_states.keys()) - backend_workspace_ids
        )
        for unknown_workspace_id in unknown_workspace_ids:
            self._request_workspace_cleanup(
                runner,
                workspace_id=unknown_workspace_id,
                reason="unknown_runtime_workspace",
            )

        for ws in backend_workspaces:
            ws_id_str = str(ws.id)
            cleanup_key = (runner_id_str, ws_id_str)
            runner_status = runner_ws_states.get(ws_id_str)
            runner_payload = runner_ws_payloads.get(ws_id_str, {})

            if ws.status in (
                WorkspaceStatus.FAILED,
                WorkspaceStatus.REMOVED,
            ):
                if runner_status is not None:
                    self._request_workspace_cleanup(
                        runner,
                        workspace_id=ws_id_str,
                        reason=f"backend_terminal_state:{ws.status}",
                    )
                else:
                    self._pending_unknown_workspace_cleanup.discard(cleanup_key)
                continue

            if ws.status in (
                WorkspaceStatus.PENDING_DELETION,
                WorkspaceStatus.DELETING,
                WorkspaceStatus.DELETE_FAILED,
                WorkspaceStatus.DELETED,
            ):
                self._cleanup_desktop_state(ws_id_str)
                continue

            # This workspace is backend-managed and non-terminal.
            self._pending_unknown_workspace_cleanup.discard(cleanup_key)

            if runner_status is None:
                # Workspace exists in backend but not on runner —
                # container was removed externally.
                if ws.status in (
                    WorkspaceStatus.RUNNING,
                    WorkspaceStatus.STOPPED,
                ):
                    logger.warning(
                        "Workspace %s missing from runner %s, marking failed",
                        ws_id_str,
                        runner.id,
                    )
                    self.workspaces.update_status(
                        ws, WorkspaceStatus.FAILED
                    )
                    self._forward_workspace_status(ws, status="failed")
                self._cleanup_desktop_state(ws_id_str)
            else:
                # Map Docker container status to workspace status
                new_status = self._map_instance_status(runner_status)
                if new_status and new_status != ws.status:
                    # Never promote CREATING → RUNNING via heartbeat.
                    # Only the explicit workspace:created event (sent after
                    # repos are cloned and SSH is established) may do that.
                    if (
                        ws.status == WorkspaceStatus.CREATING
                        and new_status == WorkspaceStatus.RUNNING
                    ):
                        continue
                    logger.info(
                        "Heartbeat: workspace %s status %s -> %s",
                        ws_id_str,
                        ws.status,
                        new_status,
                    )
                    self.workspaces.update_status(ws, new_status)
                    self._forward_workspace_status(ws, status=new_status)

                if not (
                    new_status == WorkspaceStatus.RUNNING
                    or (new_status is None and ws.status == WorkspaceStatus.RUNNING)
                ):
                    self._cleanup_desktop_state(ws_id_str)
                    continue

                if not ws.credentials_present and ws.credentials.exists():
                    try:
                        async_to_sync(self._dispatch_credential_inject)(ws)
                    except Exception:
                        logger.exception(
                            "Failed to reconcile credentials for workspace %s",
                            ws_id_str,
                        )

                if "desktop" in runner_payload:
                    self._sync_desktop_state_from_heartbeat(
                        ws_id_str,
                        runner_payload.get("desktop"),
                        runner_id=runner_id_str,
                    )

        async_to_sync(self.auto_stop_inactive_workspaces)(runner_id=runner.id)

    def handle_unknown_workspace_cleanup_result(
        self,
        runner: "Runner",
        workspace_id: str,
        *,
        cleaned: bool,
        error: str | None = None,
    ) -> None:
        """Handle result events for unknown-workspace cleanup requests."""
        cleanup_key = (str(runner.id), workspace_id)
        self._pending_unknown_workspace_cleanup.discard(cleanup_key)

        if error:
            logger.error(
                "Unknown workspace cleanup failed on runner %s (workspace=%s): %s",
                runner.id,
                workspace_id,
                error,
            )
            return

        logger.info(
            "Unknown workspace cleanup completed on runner %s (workspace=%s, cleaned=%s)",
            runner.id,
            workspace_id,
            cleaned,
        )

    def _request_workspace_cleanup(
        self,
        runner: "Runner",
        *,
        workspace_id: str,
        reason: str,
    ) -> None:
        """Request runner cleanup for a runtime workspace ID, deduplicated."""
        cleanup_key = (str(runner.id), workspace_id)
        if cleanup_key in self._pending_unknown_workspace_cleanup:
            return

        logger.warning(
            "Heartbeat: requesting cleanup for workspace %s on runner %s (%s)",
            workspace_id,
            runner.id,
            reason,
        )
        self._pending_unknown_workspace_cleanup.add(cleanup_key)
        async_to_sync(self._emit_to_runner)(
            runner,
            "task:cleanup_unknown_workspace",
            {"workspace_id": workspace_id},
        )

    @staticmethod
    def _map_instance_status(status: str) -> str | None:
        """Map a runtime instance status string to a WorkspaceStatus value.

        Supports both Docker container states and QEMU/libvirt domain states.
        """
        mapping = {
            # Docker states
            "running": WorkspaceStatus.RUNNING,
            "exited": WorkspaceStatus.STOPPED,
            "dead": WorkspaceStatus.FAILED,
            "removing": WorkspaceStatus.REMOVED,
            "created": WorkspaceStatus.CREATING,
            # QEMU/libvirt states
            "stopped": WorkspaceStatus.STOPPED,
            "failed": WorkspaceStatus.FAILED,
            "removed": WorkspaceStatus.REMOVED,
        }
        return mapping.get(status)

    # ------------------------------------------------------------------
    # Query methods (used by REST API)
    # ------------------------------------------------------------------

    def list_runners(self, organization_id: uuid.UUID | None = None) -> list["Runner"]:
        """Return all registered runners, optionally filtered by organization."""
        if organization_id:
            return list(self.runners.list_by_organization(organization_id))
        return list(self.runners.list_all())

    def get_runner(self, runner_id: uuid.UUID) -> "Runner":
        """Return a runner by ID or raise RunnerNotFoundError."""
        runner = self.runners.get_by_id(runner_id)
        if runner is None:
            raise RunnerNotFoundError(str(runner_id))
        return runner

    def update_runner_qemu_settings(
        self,
        runner_id: uuid.UUID,
        **fields,
    ) -> "Runner":
        """Update per-runner QEMU resource limits/defaults."""
        runner = self.get_runner(runner_id)
        updated_fields = dict(fields)
        if not updated_fields:
            return runner
        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=RuntimeType.QEMU,
        )
        for key, value in updated_fields.items():
            setattr(runner, key, value)
        self._validate_runner_qemu_limits(runner)
        return self.runners.update_qemu_settings(runner, **updated_fields)

    def list_workspaces(
        self,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        user=None,
    ) -> list["Workspace"]:
        """Return workspaces filtered by org and owner."""
        if runner_id:
            qs = self.workspaces.list_by_runner(runner_id)
        elif organization_id:
            qs = self.workspaces.list_by_organization(organization_id)
        else:
            qs = self.workspaces.list_all()

        if user is not None:
            qs = qs.filter(created_by=user)

        return list(qs)

    def get_workspace(self, workspace_id: uuid.UUID) -> "Workspace":
        """Return a workspace by ID or raise WorkspaceNotFoundError."""
        workspace = self.workspaces.get_by_id(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace

    def get_workspace_for_user(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
    ) -> "Workspace":
        """Return a workspace only when it belongs to the active org and owner."""
        workspace = self.get_workspace(workspace_id)
        if workspace.runner.organization_id != organization_id:
            raise WorkspaceNotFoundError(str(workspace_id))
        if workspace.created_by_id != user.id:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit_to_runner(
        self,
        runner: "Runner",
        event: str,
        data: dict,
    ) -> None:
        """Send a Socket.IO event to a specific runner by its SID."""
        if self.sio is None:
            logger.error("No Socket.IO server configured — cannot emit events")
            raise RuntimeError("Socket.IO server is not configured")

        if not runner.sid:
            logger.error(
                "Runner %s has no SID — cannot send event %s",
                runner.id,
                event,
            )
            raise RunnerOfflineError(str(runner.id))

        await self.sio.emit(event, data, to=runner.sid)
        logger.debug("Emitted %s to runner %s: %s", event, runner.id, data)

    async def _call_runner(
        self,
        runner: "Runner",
        event: str,
        data: dict,
        *,
        timeout: int = 15,
    ) -> dict:
        """Send request/response Socket.IO call to a specific runner."""
        if self.sio is None:
            raise RuntimeError("No Socket.IO server configured")
        if not runner.sid:
            raise RunnerOfflineError(str(runner.id))

        try:
            response = await self.sio.call(event, data, to=runner.sid, timeout=timeout)
        except SocketIOTimeoutError as exc:
            raise RuntimeError(
                f"Runner call timed out for event '{event}'"
            ) from exc
        if response is None:
            return {}
        if isinstance(response, dict):
            return response
        return {"result": response}

    def _route_harness_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> bool | None:
        """Route harness reply events to the owning accessor.

        Returns None when *event* is not a harness reply, True when the
        payload was routed to an accessor (or dropped after validation),
        in which case callers must not forward it to the frontend.
        """
        harness_events = {
            "harness:exec_chunk",
            "harness:exec_done",
            "harness:exec_wait_result",
            "harness:read_file_result",
            "harness:write_file_result",
            "harness:list_result",
            "harness:stat_result",
            "harness:desktop_action_result",
        }
        if event not in harness_events:
            return None
        from apps.harness.access.runner_accessor import (
            route_harness_chunk,
            route_harness_done,
            route_harness_result,
        )

        workspace_id = data.get("workspace_id", "")
        if runner_id and workspace_id:
            try:
                workspace_id_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s",
                    event,
                    workspace_id,
                )
                return True
            if not self._validate_harness_workspace_runner(
                workspace_id_uuid, runner_id
            ):
                return True
        if event == "harness:exec_chunk":
            route_harness_chunk(data)
        elif event == "harness:exec_done":
            route_harness_done(data)
        else:
            route_harness_result(data)
        return True

    def _validate_harness_workspace_runner(
        self,
        workspace_id: uuid.UUID,
        runner_id: str,
    ) -> bool:
        """Return True when *workspace_id* belongs to *runner_id* (sync)."""
        try:
            claimed_runner_id = uuid.UUID(str(runner_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "harness reply rejected: invalid runner_id %s",
                runner_id,
            )
            return False
        owner_id = self.workspaces.get_runner_id(workspace_id)
        if owner_id is None:
            logger.warning(
                "harness reply rejected: workspace %s not found",
                workspace_id,
            )
            return False
        if owner_id != claimed_runner_id:
            logger.warning(
                "harness reply rejected: workspace %s not owned by %s",
                workspace_id,
                runner_id,
            )
            return False
        return True

    async def emit_harness_event(
        self,
        runner: "Runner",
        event: str,
        payload: dict,
    ) -> None:
        """Emit a harness RPC event to the runner owning a workspace."""
        await self._emit_to_runner(runner, event, payload)

    def handle_harness_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Route a harness reply from a runner to its accessor (sync).

        Called from Socket.IO handlers via ``sync_to_async``. Harness
        operations never touch the frontend event bus.
        """
        self._route_harness_reply(event, data, runner_id=runner_id)

    def _forward_to_frontend(        self,
        event: str,
        data: dict,
        workspace_id: str,
    ) -> None:
        """
        Schedule a Socket.IO emit to subscribed frontend clients.

        Since service methods are called synchronously (via sync_to_async),
        this schedules the async emit on the running event loop.
        """
        from .sio_server import emit_to_frontend

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                async_to_sync(emit_to_frontend)(event, data, workspace_id)
            else:
                loop.create_task(emit_to_frontend(event, data, workspace_id))
        except Exception:
            logger.exception(
                "Failed forwarding event to frontend",
                extra={
                    "event": event,
                    "workspace_id": workspace_id,
                },
            )

    # ------------------------------------------------------------------
    # Image definition operations
    # ------------------------------------------------------------------

    @staticmethod
    def _build_package_install_block(base_distro: str, packages: list[str]) -> str:
        """Generate package installation block based on distro family."""
        clean_packages = [p.strip() for p in packages if p.strip()]
        if not clean_packages:
            return ""

        distro = (base_distro or "").lower()
        if "alpine" in distro:
            return "RUN apk add --no-cache " + " ".join(clean_packages)

        # Default to apt for ubuntu/debian and unknown distros.
        return (
            "RUN apt-get update && apt-get install -y \\\n"
            f"    {' '.join(clean_packages)} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

    @staticmethod
    def _validate_qemu_base_distro(base_distro: str) -> None:
        """Ensure QEMU image definitions use a supported distro source."""
        distro = (base_distro or "").strip().lower()
        if distro.startswith("ubuntu:"):
            return
        raise ConflictError(
            "QEMU image definitions currently require an ubuntu:<version> base distro"
        )

    @staticmethod
    def _desktop_session_dockerfile_block() -> str:
        """Return Dockerfile lines that install KasmVNC desktop session support."""
        return """# --- KasmVNC desktop session support ---
RUN apt-get update && apt-get install -y \\
    xfonts-base openbox dbus-x11 x11-xserver-utils ffmpeg xdotool \\
    libnss3 libatk-bridge2.0-0 libcups2 libdrm2 \\
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \\
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \\
    wget ca-certificates \\
    && (apt-get install -y libasound2t64 || apt-get install -y libasound2) \\
    && wget -q -O /tmp/kasmvnc.deb \\
       "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_jammy_1.3.3_amd64.deb" \\
    && apt-get install -y /tmp/kasmvnc.deb || true \\
    && apt-get install -f -y \\
    && rm -f /tmp/kasmvnc.deb \\
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \\
    && (apt-get install -y /tmp/google-chrome.deb || apt-get install -f -y) \\
    && rm -f /tmp/google-chrome.deb \\
    && rm -rf /var/lib/apt/lists/*

# Pre-configure KasmVNC (skip interactive wizard)
RUN mkdir -p /root/.vnc \\
    && touch /root/.vnc/.de-was-selected \\
    && printf "password\\npassword\\n" | vncpasswd -u root -w -r 2>/dev/null || true \\
    && printf 'desktop:\\n  resolution:\\n    width: 1920\\n    height: 1080\\n  allow_resize: true\\nnetwork:\\n  protocol: http\\n  interface: 0.0.0.0\\n  websocket_port: 6901\\n  ssl:\\n    require_ssl: false\\n    pem_certificate:\\n    pem_key:\\n' > /root/.vnc/kasmvnc.yaml \\
    && printf '#!/bin/bash\\nset -eu\\nfor browser in google-chrome-stable google-chrome chromium chromium-browser /usr/lib/chromium/chromium; do\\n  if [ \"${browser#/}\" != \"$browser\" ]; then\\n    if [ -x \"$browser\" ]; then\\n      exec \"$browser\" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run\\n    fi\\n    continue\\n  fi\\n  if command -v \"$browser\" >/dev/null 2>&1; then\\n    if [ \"$browser\" = \"chromium-browser\" ] && ! chromium-browser --version >/dev/null 2>&1; then\\n      continue\\n    fi\\n    exec \"$browser\" --no-sandbox --disable-gpu --start-maximized --disable-dev-shm-usage --no-first-run\\n  fi\\ndone\\necho \"No supported browser binary found for desktop session\" >&2\\n' > /usr/local/bin/opencuria-desktop-browser \\
    && printf '#!/bin/bash\\nexport DISPLAY=:1\\nexport HOME=/root\\nopenbox-session &\\nsleep 1\\n/usr/local/bin/opencuria-desktop-browser >/root/.vnc/browser.log 2>&1 &\\nwait\\n' > /root/.vnc/xstartup \\
    && chmod +x /root/.vnc/xstartup /usr/local/bin/opencuria-desktop-browser

# Desktop start/stop scripts (use Xvnc directly to avoid KasmVNC perl wrapper prompts)
RUN printf '#!/bin/bash\\nset -e\\nexport DISPLAY=:1\\nexport HOME=/root\\n/usr/local/bin/opencuria-desktop-stop 2>/dev/null || true\\nmkdir -p /root/.vnc\\nrm -f /tmp/.X1-lock /tmp/.X11-unix/X1\\n/usr/bin/Xvnc :1 -geometry 1920x1080 -depth 24 -rfbport 5901 -SecurityTypes None -disableBasicAuth -websocketPort 6901 -httpd /usr/share/kasmvnc/www -interface 0.0.0.0 -AlwaysShared -AcceptKeyEvents -AcceptPointerEvents -AcceptSetDesktopSize -SendCutText -AcceptCutText >>/root/.vnc/server.log 2>&1 &\\nfor _ in $(seq 1 120); do\\n  if [ -e /tmp/.X11-unix/X1 ]; then\\n    /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &\\n    echo \"Desktop session started on :1 (ws port 6901)\"\\n    exit 0\\n  fi\\n  sleep 0.25\\ndone\\necho \"Desktop session failed to start\" >&2\\nexit 1\\n' > /usr/local/bin/opencuria-desktop-start \
    && printf '#!/bin/bash\\nfor pid in $(pgrep -f "Xvnc.*:1" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done\\nfor pid in $(pgrep -f "openbox" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done\\nrm -f /tmp/.X1-lock /tmp/.X11-unix/X1\\n' > /usr/local/bin/opencuria-desktop-stop \
    && chmod +x /usr/local/bin/opencuria-desktop-start /usr/local/bin/opencuria-desktop-stop
"""

    @staticmethod
    def _desktop_session_init_script_block() -> str:
        """Return shell script lines that install the QEMU KasmVNC desktop."""
        script_path = (
            Path(__file__).resolve().parent / "scripts" / "qemu_desktop_session.sh"
        )
        return "\n" + script_path.read_text(encoding="utf-8").strip() + "\n"

    @classmethod
    def _build_qemu_init_script_content(cls, definition) -> str:
        """Build a shell init script for QEMU image definitions."""
        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "",
        ]

        packages = [p.strip() for p in list(definition.packages or []) if p.strip()]
        distro = (definition.base_distro or "").lower()
        if packages:
            if "alpine" in distro:
                lines += [
                    f"apk add --no-cache {' '.join(packages)}",
                    "",
                ]
            else:
                lines += [
                    "export DEBIAN_FRONTEND=noninteractive",
                    "apt-get update",
                    f"apt-get install -y {' '.join(packages)}",
                    "rm -rf /var/lib/apt/lists/*",
                    "",
                ]

        env_vars = dict(definition.env_vars or {})
        if env_vars:
            lines += [
                "cat >/etc/profile.d/opencuria-image-env.sh <<'EOF'",
                "#!/bin/sh",
            ]
            for key, value in env_vars.items():
                if key:
                    escaped = str(value).replace('"', '\\"')
                    lines.append(f'export {key}="{escaped}"')
            lines += [
                "EOF",
                "chmod 644 /etc/profile.d/opencuria-image-env.sh",
                "",
            ]

        custom_script = (definition.custom_init_script or "").strip()
        if custom_script:
            lines += [
                "# Custom image definition steps",
                custom_script,
                "",
            ]

        # Always include KasmVNC desktop session support (non-Alpine only)
        distro_check = (definition.base_distro or "").lower()
        if "alpine" not in distro_check:
            lines += [cls._desktop_session_init_script_block(), ""]

        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _generate_dockerfile_content(cls, definition) -> str:
        """Build Dockerfile content from an image definition record."""
        lines = [f"FROM {definition.base_distro}", ""]

        if "alpine" not in (definition.base_distro or "").lower():
            lines += ["ENV DEBIAN_FRONTEND=noninteractive", ""]

        install_block = cls._build_package_install_block(
            definition.base_distro, list(definition.packages or [])
        )
        if install_block:
            lines += [install_block, ""]

        for key, value in dict(definition.env_vars or {}).items():
            if key:
                lines.append(f"ENV {key}={value}")
        if definition.env_vars:
            lines.append("")

        if definition.custom_dockerfile:
            lines += [definition.custom_dockerfile.strip(), ""]

        # Always include KasmVNC desktop session support (non-Alpine only)
        if "alpine" not in (definition.base_distro or "").lower():
            lines += [cls._desktop_session_dockerfile_block(), ""]

        lines += [
            'CMD ["tail", "-f", "/dev/null"]',
        ]
        return "\n".join(lines).strip() + "\n"

    def list_image_definitions(self, organization_id: uuid.UUID) -> list:
        """List image definitions for an organization."""
        self.timeout_stale_image_operations()
        return list(self.image_definitions.list_by_org(organization_id))

    def list_build_jobs(
        self,
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> list:
        """List runner build records for an image definition."""
        self.timeout_stale_image_operations()
        return list(
            self.build_jobs.list_for_definition(
                image_definition_id,
                organization_id=organization_id,
            )
        )

    def timeout_stale_image_operations(self, *, timeout_hours: int = 1) -> None:
        """Fail hung builds and stuck deletions so the UI can retry."""
        from .models import ImageDefinition, ImageInstance

        cutoff = timezone.now() - timedelta(hours=timeout_hours)
        stale_message = f"Timed out after {timeout_hours}h without progress"

        def _instance_or_none(build):
            try:
                return build.image_instance
            except ImageInstance.DoesNotExist:
                return None

        for build in self.build_jobs.list_stale_builds(cutoff=cutoff):
            self.build_jobs.mark_failed(build.id, error=stale_message)
            instance = _instance_or_none(build)
            if instance is not None and instance.status in {
                ImageInstance.Status.BUILDING,
                ImageInstance.Status.CAPTURING,
            }:
                self.image_instances.mark_failed(instance.id)

        for build in self.build_jobs.list_stale_deletes(cutoff=cutoff):
            self.build_jobs.mark_delete_failed(build.id, error=stale_message)
            instance = _instance_or_none(build)
            if instance is not None and instance.status in {
                ImageInstance.Status.PENDING_DELETION,
                ImageInstance.Status.DELETING,
            }:
                self.image_instances.mark_delete_failed(
                    instance.id, error=stale_message
                )
            self._mark_definition_delete_failed(
                build.image_definition_id,
                error=stale_message,
            )

        deleting_definitions = ImageDefinition.objects.filter(
            status__in=[
                ImageDefinition.Status.PENDING_DELETION,
                ImageDefinition.Status.DELETING,
            ]
        )
        for definition in deleting_definitions:
            self._check_definition_deletion_complete(definition.id)
            definition.refresh_from_db()
            if definition.status not in {
                ImageDefinition.Status.PENDING_DELETION,
                ImageDefinition.Status.DELETING,
            }:
                continue
            if self.build_jobs.list_in_progress_deletes_for_definition(
                definition.id
            ).exists():
                continue
            self._mark_definition_delete_failed(
                definition.id,
                error=stale_message,
            )

    def _ensure_definition_mutable(self, definition) -> None:
        """Reject runner mutations while a definition is deleted or being removed."""
        from .models import ImageDefinition

        if definition.status in {
            ImageDefinition.Status.PENDING_DELETION,
            ImageDefinition.Status.DELETING,
            ImageDefinition.Status.DELETED,
            ImageDefinition.Status.DELETE_FAILED,
        }:
            raise ConflictError(
                f"Cannot modify image definition in state '{definition.status}'"
            )

    async def activate_build_job(self, build, *, created_by=None):
        """Make an existing runner image selectable, or build it if none exists."""
        from .models import ImageBuildJob, ImageInstance

        self._ensure_definition_mutable(build.image_definition)
        instance = await sync_to_async(self.image_instances.get_by_build_job_id)(
            build.id
        )
        has_ready_image = (
            build.built_at is not None
            and instance is not None
            and instance.status
            in {ImageInstance.Status.READY, ImageInstance.Status.RETIRED}
            and bool(instance.runner_ref)
        )
        if not has_ready_image:
            return await self.trigger_build_job(
                image_definition=build.image_definition,
                runner=build.runner,
                activate=True,
                created_by=created_by,
            )

        build.status = ImageBuildJob.Status.ACTIVE
        build.deactivated_at = None
        await sync_to_async(build.save)(
            update_fields=["status", "deactivated_at", "updated_at"]
        )
        if instance.status == ImageInstance.Status.RETIRED:
            await sync_to_async(self.image_instances.mark_ready_from_retired)(
                instance.id
            )
        return build

    async def trigger_build_job(
        self,
        *,
        image_definition,
        runner,
        activate: bool = True,
        created_by=None,
    ):
        """Create/update runner build record and dispatch task:build_image."""
        from .models import ImageInstance, ImageBuildJob

        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=image_definition.runtime_type,
        )
        self._ensure_definition_mutable(image_definition)

        existing = await sync_to_async(self.build_jobs.get)(
            image_definition.id, runner.id
        )
        if existing is not None and existing.status in {
            ImageBuildJob.Status.PENDING_DELETION,
            ImageBuildJob.Status.DELETING,
        }:
            raise ConflictError(
                f"Build job is already in deletion state '{existing.status}'"
            )

        if existing is None:
            build = await sync_to_async(ImageBuildJob.objects.create)(
                image_definition=image_definition,
                runner=runner,
                status=ImageBuildJob.Status.PENDING,
            )
        else:
            build = existing
            build.status = (
                ImageBuildJob.Status.DEACTIVATED
                if not activate
                else ImageBuildJob.Status.PENDING
            )
            build.build_task = None
            build.deleting_task_id = None
            build.delete_requested_at = None
            build.delete_started_at = None
            build.delete_confirmed_at = None
            build.delete_last_error = ""
            await sync_to_async(build.save)(
                update_fields=[
                    "status",
                    "build_task",
                    "deleting_task_id",
                    "delete_requested_at",
                    "delete_started_at",
                    "delete_confirmed_at",
                    "delete_last_error",
                    "updated_at",
                ]
            )

        if not activate:
            existing_image = await sync_to_async(
                self.image_instances.get_by_build_job_id
            )(build.id)
            if existing_image is not None:
                await sync_to_async(self.image_instances.mark_retired)(existing_image.id)
            return build

        if not runner.sid:
            logger.info(
                "Runner %s is offline; leaving image build %s pending",
                runner.id,
                build.id,
            )
            return build

        if image_definition.runtime_type == RuntimeType.QEMU:
            self._validate_qemu_base_distro(image_definition.base_distro)

        task = await sync_to_async(self.tasks.create)(
            task_id=generate_uuid(),
            runner=runner,
            task_type=TaskType.BUILD_IMAGE,
        )
        build_runner_ref = (
            f"opencuria/custom/{re.sub(r'[^a-z0-9-]+', '-', image_definition.name.lower())}:{build.id}"
            if image_definition.runtime_type == RuntimeType.DOCKER
            else f"/var/lib/opencuria/base-images/{build.id}.qcow2"
        )
        await sync_to_async(
            ImageBuildJob.objects.filter(id=build.id).update
        )(
            build_task=task,
            status=ImageBuildJob.Status.PENDING,
        )
        image = await sync_to_async(
            self.image_instances.get_by_build_job_id
        )(build.id)
        image_name = f"{image_definition.name} ({runner.name})"
        if image is None:
            await sync_to_async(self.image_instances.create_pending)(
                runner=runner,
                runtime_type=image_definition.runtime_type,
                origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
                origin_definition=image_definition,
                name=image_name,
                creating_task_id=str(task.id),
                build_job=build,
                created_by=created_by,
            )
        else:
            image.name = image_name
            image.status = ImageInstance.Status.BUILDING
            image.created_by = created_by
            image.creating_task_id = str(task.id)
            image.size_bytes = 0
            image.origin_definition = image_definition
            image.runner = runner
            image.runtime_type = image_definition.runtime_type
            await sync_to_async(image.save)(
                update_fields=[
                    "name",
                    "status",
                    "created_by",
                    "creating_task_id",
                    "size_bytes",
                    "origin_definition",
                    "runner",
                    "runtime_type",
                ]
            )

        build = await sync_to_async(ImageBuildJob.objects.select_related(
            "image_definition", "runner", "build_task"
        ).get)(id=build.id)

        payload = {
            "task_id": str(task.id),
            "build_job_id": str(build.id),
            "runtime_type": image_definition.runtime_type,
        }
        if image_definition.runtime_type == RuntimeType.DOCKER:
            payload["dockerfile_content"] = self._generate_dockerfile_content(
                image_definition
            )
            payload["image_tag"] = build_runner_ref
        else:
            payload["base_distro"] = image_definition.base_distro
            payload["init_script"] = self._build_qemu_init_script_content(
                image_definition
            )
            payload["image_path"] = build_runner_ref

        await self._emit_to_runner(runner, "task:build_image", payload)
        await sync_to_async(self.tasks.mark_in_progress)(task)
        return build

    def handle_image_build_progress(
        self, build_job_id: str, line: str, runner_id: str | None = None
    ) -> None:
        """Append build log lines for runner image builds."""
        from .models import ImageBuildJob

        try:
            build = ImageBuildJob.objects.select_related("runner").get(
                id=build_job_id
            )
        except ImageBuildJob.DoesNotExist:
            return
        if runner_id and str(build.runner_id) != str(runner_id):
            return
        build.status = ImageBuildJob.Status.BUILDING
        build.build_log = (build.build_log or "") + (line.rstrip("\n") + "\n")
        build.save(update_fields=["status", "build_log", "updated_at"])

    def handle_image_built(
        self,
        *,
        task_id: str,
        build_job_id: str,
        image_tag: str = "",
        image_path: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as active and complete its task."""
        from django.utils import timezone
        from .models import ImageInstance, ImageBuildJob

        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        if not self._validate_task_runner(task, runner_id):
            return

        build = ImageBuildJob.objects.get(id=build_job_id)
        build.status = ImageBuildJob.Status.ACTIVE
        build.built_at = timezone.now()
        build.save(
            update_fields=["status", "built_at", "updated_at"]
        )
        image = self.image_instances.get_by_build_job_id(
            uuid.UUID(build_job_id)
        )
        runner_ref = image_tag or image_path
        image_name = f"{build.image_definition.name} ({build.runner.name})"
        if image is None:
            self.image_instances.create(
                runner=build.runner,
                runtime_type=build.image_definition.runtime_type,
                origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
                origin_definition=build.image_definition,
                runner_ref=runner_ref,
                name=image_name,
                size_bytes=0,
                build_job=build,
            )
        else:
            image.name = image_name
            image.status = ImageInstance.Status.READY
            image.runner_ref = runner_ref
            image.size_bytes = 0
            image.creating_task_id = None
            image.deleted_at = None
            image.save(
                update_fields=[
                    "name",
                    "status",
                    "runner_ref",
                    "size_bytes",
                    "creating_task_id",
                    "deleted_at",
                ]
            )
        self.tasks.complete(task)

    def handle_image_build_failed(
        self,
        *,
        task_id: str,
        build_job_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as failed and fail the correlated task."""
        from .models import ImageBuildJob

        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.FAILED
        )
        image = self.image_instances.get_by_build_job_id(
            uuid.UUID(build_job_id)
        )
        if image is not None:
            self.image_instances.mark_failed(image.id)
        if task is not None:
            self.tasks.fail(task, error)

    # ------------------------------------------------------------------
    # Image artifact operations
    # ------------------------------------------------------------------

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
        organization_id: uuid.UUID | None = None,
    ) -> tuple["Workspace", "Task"]:
        """Dispatch image artifact creation to the runner.

        Creates a pending image instance immediately so the UI can show
        progress. The record is updated to 'ready' when the runner completes.
        """
        from .models import ImageInstance

        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        if organization_id and workspace.runner.organization_id != organization_id:
            raise WorkspaceNotFoundError(str(workspace_id))
        self._ensure_workspace_available(workspace)

        if workspace.status not in (
            WorkspaceStatus.RUNNING,
            WorkspaceStatus.STOPPED,
        ):
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                "must be running or stopped to capture an image"
            )
        if workspace.credentials_present:
            raise ConflictError(
                "Workspace still has credentials on disk and cannot be captured. "
                "Stop the workspace to remove them first. If it was stopped "
                "externally, resume it and stop it again."
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        # Verify runtime supports image artifact capture
        if workspace.runtime_type not in (runner.available_runtimes or []):
            raise ValueError(
                f"Runner does not support runtime '{workspace.runtime_type}'"
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.CREATE_IMAGE_ARTIFACT,
            workspace=workspace,
        )

        # Create the artifact record upfront so the UI can immediately show the
        # 'creating' state.
        created_by = await sync_to_async(lambda: workspace.created_by)()
        image = await sync_to_async(self.image_instances.create_pending)(
            runner=runner,
            runtime_type=workspace.runtime_type,
            origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
            origin_workspace=workspace,
            name=name,
            creating_task_id=str(task_id),
            created_by=created_by,
        )

        await self._dispatch_workspace_task(
            runner=runner,
            event="task:create_image_artifact",
            task=task,
            workspace=workspace,
            operation=self._task_workspace_operation(TaskType.CREATE_IMAGE_ARTIFACT),
            payload={
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "name": name,
            },
        )
        logger.info(
            "Dispatched create_image_artifact (workspace=%s, task=%s, image=%s)",
            workspace_id,
            task_id,
            image.id,
        )
        return workspace, task

    def handle_image_artifact_created(
        self,
        task_id: str,
        workspace_id: str,
        artifact_id: str,
        name: str,
        size_bytes: int = 0,
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:created from runner and mark the image ready."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            raise TaskNotFoundError(task_id)

        if not self._validate_task_runner(task, runner_id):
            return

        image = self.image_instances.get_by_task_id(task_id)
        if image is not None:
            self.image_instances.mark_ready(
                image.id,
                runner_ref=artifact_id,
                size_bytes=size_bytes,
            )
        else:
            workspace = self.workspaces.get_by_id(uuid.UUID(workspace_id))
            if workspace is None:
                raise WorkspaceNotFoundError(workspace_id)
            self.image_instances.create(
                runner=workspace.runner,
                runtime_type=workspace.runtime_type,
                origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
                origin_workspace=workspace,
                runner_ref=artifact_id,
                name=name,
                size_bytes=size_bytes,
                created_by=task.workspace.created_by if task.workspace else None,
            )

        if task.workspace:
            self.workspaces.update_active_operation(task.workspace, None)
        self.tasks.complete(task)
        logger.info(
            "Image artifact created: workspace=%s, artifact=%s",
            workspace_id,
            artifact_id,
        )

        self._forward_to_frontend(
            "image_artifact:created",
            {
                "workspace_id": workspace_id,
                "image_artifact_id": artifact_id,
                "name": name,
                "size_bytes": size_bytes,
            },
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_image_artifact_failed(
        self,
        task_id: str,
        workspace_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:failed by marking the pending image as failed."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        self.image_instances.mark_failed_by_task_id(task_id)

        if task is not None:
            if task.workspace:
                self.workspaces.update_active_operation(task.workspace, None)
            self.tasks.fail(task, error)

        logger.warning(
            "Image artifact creation failed: workspace=%s, task=%s, error=%s",
            workspace_id,
            task_id,
            error,
        )

        self._forward_to_frontend(
            "image_artifact:failed",
            {"workspace_id": workspace_id, "task_id": task_id, "error": error},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def list_image_artifacts_for_workspace(self, workspace_id: uuid.UUID) -> list:
        """Return all artifacts captured from a workspace."""
        return list(self.image_instances.list_by_workspace(workspace_id))

    def list_image_artifacts_for_user(self, user) -> list:
        """Return all artifacts created by a specific user."""
        return list(self.image_instances.list_by_user(user))

    async def delete_image_artifact(
        self,
        image_artifact_id: uuid.UUID,
    ) -> None:
        """Delete an image instance safely and dispatch cleanup to the runner if needed."""
        from .models import ImageInstance
        image = await sync_to_async(self.image_instances.get_by_id)(image_artifact_id)
        if image is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' not found")

        if image.status in (
            ImageInstance.Status.PENDING_DELETION,
            ImageInstance.Status.DELETING,
            ImageInstance.Status.DELETED,
        ):
            raise ConflictError(
                f"Image artifact '{image_artifact_id}' is already in deletion state '{image.status}'"
            )

        dependent_workspaces = await sync_to_async(
            lambda: list(self.workspaces.list_by_base_image_instance(image_artifact_id))
        )()
        if dependent_workspaces:
            raise ConflictError(
                f"Image artifact '{image_artifact_id}' is still used by {len(dependent_workspaces)} workspace(s)"
            )

        # Built images can only be deleted via their build job
        if image.origin_type == ImageInstance.OriginType.DEFINITION_BUILD and image.build_job_id:
            raise ConflictError(
                f"Built image artifact '{image_artifact_id}' can only be deleted via its runner build job"
            )

        runner = image.runner
        if not image.runner_ref:
            if image.status in (
                ImageInstance.Status.BUILDING,
                ImageInstance.Status.CAPTURING,
            ):
                raise ConflictError(
                    f"Image artifact '{image_artifact_id}' cannot be deleted while it is still {image.status}"
                )
            await sync_to_async(self.image_instances.mark_deleted)(image_artifact_id)
            logger.info("Image artifact deleted without runner cleanup: %s", image_artifact_id)
            return

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.DELETE_IMAGE,
        )

        if runner.is_online:
            await sync_to_async(self.image_instances.mark_deleting)(
                image_artifact_id,
                deleting_task_id=str(task.id),
            )
            await self._emit_to_runner(
                runner,
                "task:delete_image_artifact",
                {
                    "task_id": str(task.id),
                    "image_instance_id": str(image.id),
                    "runtime_type": image.runtime_type,
                    "image_artifact_id": image.runner_ref,
                },
            )
            await sync_to_async(self.tasks.mark_in_progress)(task)
        else:
            await sync_to_async(self.image_instances.mark_pending_deletion)(image_artifact_id)

        logger.info("Image artifact marked for deletion: %s", image_artifact_id)

    def handle_image_artifact_deleted(
        self,
        task_id: str,
        image_instance_id: str = "",
        runner_ref: str = "",
        result: str = "deleted",
        runner_id: str | None = None,
    ) -> None:
        """Mark an image instance deleted after runner cleanup confirms it."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        if result not in {"deleted", "already_absent"}:
            self.handle_image_artifact_delete_failed(
                task_id=task_id,
                error=f"Delete was not confirmed: {result}",
                runner_id=runner_id,
            )
            return

        image = None
        if image_instance_id:
            image = self.image_instances.get_by_id(uuid.UUID(image_instance_id))
        if image is None and task_id:
            image = self.image_instances.get_by_task_id(task_id)
        if image is None and runner_ref and runner_id:
            pending = list(
                self.image_instances.list_pending_delete_for_runner(uuid.UUID(runner_id))
            )
            image = next((item for item in pending if item.runner_ref == runner_ref), None)

        if image is not None:
            self.image_instances.mark_deleted(image.id)
        if task is not None:
            self.tasks.complete(task)

    def handle_image_artifact_delete_failed(
        self,
        task_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Handle image artifact deletion failure from runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        image = self.image_instances.get_by_task_id(task_id) if task_id else None
        if image is not None:
            self.image_instances.mark_delete_failed(image.id, error=error)
            if image.build_job_id:
                self.build_jobs.mark_delete_failed(image.build_job_id, error=error)
                self._mark_definition_delete_failed(
                    image.origin_definition_id,
                    error=error or "Runner build cleanup failed",
                )
        elif task_id:
            build = self._get_build_by_delete_task(task_id)
            if build is not None:
                self.build_jobs.mark_delete_failed(build.id, error=error)
                self._mark_definition_delete_failed(
                    build.image_definition_id,
                    error=error or "Runner build cleanup failed",
                )
        if task is not None:
            self.tasks.fail(task, error=error or "Delete failed on runner")
        logger.warning("Image artifact delete failed: task=%s error=%s", task_id, error)

    # ------------------------------------------------------------------
    # Build job deletion
    # ------------------------------------------------------------------

    async def delete_build_job(self, build_job_id: uuid.UUID) -> None:
        """Delete a runner image build and its associated artifact.

        Checks workspace dependencies before allowing deletion.
        If runner is offline, queues as pending_deletion.
        """
        from .models import ImageBuildJob

        build = await sync_to_async(self.build_jobs.get_by_id)(build_job_id)
        if build is None:
            raise ValueError(f"Build job '{build_job_id}' not found")

        if build.status in (
            ImageBuildJob.Status.PENDING_DELETION,
            ImageBuildJob.Status.DELETING,
            ImageBuildJob.Status.DELETED,
        ):
            raise ConflictError(
                f"Build job '{build_job_id}' is already in deletion state '{build.status}'"
            )

        has_deps, dep_count = await sync_to_async(
            self.build_jobs.has_dependent_workspaces
        )(build_job_id)
        if has_deps:
            raise ConflictError(
                f"Build job '{build_job_id}' is still used by {dep_count} workspace(s)"
            )

        runner = build.runner
        instance = await sync_to_async(lambda: getattr(build, "image_instance", None))()

        if instance and instance.runner_ref and runner.is_online:
            task_id = generate_uuid()
            task = await sync_to_async(self.tasks.create)(
                task_id=task_id,
                runner=runner,
                task_type=TaskType.DELETE_IMAGE,
            )
            await sync_to_async(self._mark_definition_deleting_if_needed)(
                build.image_definition_id
            )
            await sync_to_async(self.build_jobs.mark_deleting)(
                build_job_id, deleting_task_id=str(task.id)
            )
            if instance:
                await sync_to_async(self.image_instances.mark_deleting)(
                    instance.id, deleting_task_id=str(task.id)
                )
            await self._emit_to_runner(
                runner,
                "task:delete_image_artifact",
                {
                    "task_id": str(task.id),
                    "image_instance_id": str(instance.id) if instance else "",
                    "runtime_type": build.image_definition.runtime_type,
                    "image_artifact_id": instance.runner_ref if instance else "",
                },
            )
            await sync_to_async(self.tasks.mark_in_progress)(task)
        elif instance and instance.runner_ref:
            # Runner offline
            await sync_to_async(self.build_jobs.mark_pending_deletion)(build_job_id)
            if instance:
                await sync_to_async(self.image_instances.mark_pending_deletion)(instance.id)
        else:
            # No physical artifact to clean up
            await sync_to_async(self.build_jobs.mark_deleted)(build_job_id)
            if instance:
                await sync_to_async(self.image_instances.mark_deleted)(instance.id)

        logger.info("Build job marked for deletion: %s", build_job_id)

    def handle_build_job_deleted(
        self,
        task_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle successful build job deletion from runner.

        Marks both the build job and its image instance as deleted.
        Also checks if the parent definition can be marked deleted.
        """
        from .models import ImageBuildJob

        task = self.tasks.get_by_id(uuid.UUID(task_id)) if task_id else None
        if task is not None and not self._validate_task_runner(task, runner_id):
            return

        # Find build job by task_id
        build = self._get_build_by_delete_task(task_id)

        if build is not None:
            self.build_jobs.mark_deleted(build.id)
            instance = getattr(build, "image_instance", None)
            if instance:
                self.image_instances.mark_deleted(instance.id)
            # Check if parent definition can be marked deleted
            self._check_definition_deletion_complete(build.image_definition_id)

        if task is not None:
            self.tasks.complete(task)

    def _check_definition_deletion_complete(self, definition_id: uuid.UUID) -> None:
        """Check if all builds for a definition are deleted and finalize."""
        from .models import ImageDefinition

        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status not in (
            ImageDefinition.Status.PENDING_DELETION,
            ImageDefinition.Status.DELETING,
        ):
            return

        remaining = self.build_jobs.list_non_deleted_for_definition(definition_id)
        if not remaining.exists():
            self.image_definitions.mark_deleted(definition_id)
            logger.info("Definition fully deleted: %s", definition_id)

    def _get_build_by_delete_task(self, task_id: str):
        """Return the build job currently linked to a delete task, if any."""
        from .models import ImageBuildJob

        return (
            ImageBuildJob.objects.filter(deleting_task_id=task_id)
            .select_related("image_definition", "image_instance")
            .first()
        )

    def _mark_definition_delete_failed(
        self,
        definition_id: uuid.UUID | None,
        *,
        error: str,
    ) -> None:
        """Move a definition delete flow into DELETE_FAILED when a child cleanup fails."""
        if definition_id is None:
            return
        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status not in {
            definition.Status.PENDING_DELETION,
            definition.Status.DELETING,
            definition.Status.DELETE_FAILED,
        }:
            return
        self.image_definitions.mark_delete_failed(definition_id, error=error)

    def _mark_definition_deleting_if_needed(self, definition_id: uuid.UUID | None) -> None:
        """Promote a pending definition delete to deleting once runner cleanup starts."""
        if definition_id is None:
            return
        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status in {
            definition.Status.PENDING_DELETION,
            definition.Status.DELETE_FAILED,
        }:
            self.image_definitions.mark_deleting(definition_id)

    # ------------------------------------------------------------------
    # Image definition lifecycle
    # ------------------------------------------------------------------

    async def deactivate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Deactivate a definition — immediately not selectable for new workspaces."""
        definition = await sync_to_async(self.image_definitions.get_by_id)(definition_id)
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")
        await sync_to_async(self.image_definitions.deactivate)(definition_id)
        logger.info("Image definition deactivated: %s", definition_id)

    async def activate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Re-activate a deactivated definition."""
        from .models import ImageDefinition
        definition = await sync_to_async(self.image_definitions.get_by_id)(definition_id)
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")
        if definition.status not in (
            ImageDefinition.Status.DEACTIVATED,
            ImageDefinition.Status.ACTIVE,
            ImageDefinition.Status.DELETE_FAILED,
        ):
            raise ConflictError(
                f"Cannot activate definition in state '{definition.status}'"
            )
        if definition.status == ImageDefinition.Status.DELETE_FAILED:
            in_progress = await sync_to_async(
                lambda: self.build_jobs.list_in_progress_deletes_for_definition(
                    definition_id
                ).exists()
            )()
            if in_progress:
                raise ConflictError(
                    "Cannot restore while runner image removal is still in progress"
                )
        await sync_to_async(self.image_definitions.activate)(definition_id)
        logger.info("Image definition activated: %s", definition_id)

    async def delete_image_definition(self, definition_id: uuid.UUID) -> None:
        """Orchestrated two-step definition delete.

        Step 1: Immediately deactivate.
        Step 2: Initiate deletion of all runner builds.
        Definition itself is only marked deleted when all build deletes are confirmed.
        """
        from .models import ImageDefinition, ImageBuildJob

        definition = await sync_to_async(self.image_definitions.get_by_id)(definition_id)
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")

        if definition.status == ImageDefinition.Status.DELETED:
            raise ConflictError("Definition is already deleted")
        if definition.status == ImageDefinition.Status.DELETING:
            raise ConflictError("Definition deletion is already in progress")

        # Step 1: Deactivate
        await sync_to_async(self.image_definitions.deactivate)(definition_id)

        # Get all non-deleted builds for this definition
        builds = await sync_to_async(
            lambda: list(self.build_jobs.list_non_deleted_for_definition(definition_id))
        )()

        if not builds:
            # No builds -> mark definition deleted directly
            await sync_to_async(self.image_definitions.mark_deleted)(definition_id)
            logger.info("Definition deleted (no builds): %s", definition_id)
            return

        # Step 2: Mark definition as pending deletion and initiate build deletes
        await sync_to_async(self.image_definitions.mark_pending_deletion)(definition_id)

        initiation_errors: list[str] = []
        for build in builds:
            if build.status == ImageBuildJob.Status.DELETED:
                continue
            try:
                await self.delete_build_job(build.id)
            except (ConflictError, ValueError) as e:
                initiation_errors.append(str(e))
                logger.warning(
                    "Could not initiate build job deletion %s: %s", build.id, e
                )

        if initiation_errors:
            error = "; ".join(initiation_errors)
            await sync_to_async(self.image_definitions.mark_delete_failed)(
                definition_id,
                error=error,
            )
            raise ConflictError(error)

        # Check if all are already done
        await sync_to_async(self._check_definition_deletion_complete)(definition_id)

    async def dispatch_pending_workspace_deletions(self, runner: "Runner") -> list:
        """Dispatch pending workspace deletions that accumulated while runner was offline."""
        from .models import Workspace

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
                from .models import Task as TaskModel
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
                    ws.id, runner.id,
                )
        return dispatched

    async def dispatch_pending_build_job_deletions(self, runner: "Runner") -> list:
        """Dispatch pending build job deletions that accumulated while runner was offline."""
        from .models import ImageBuildJob, ImageInstance

        pending = await sync_to_async(
            lambda: list(self.build_jobs.list_pending_delete_for_runner(runner.id))
        )()

        dispatched = []
        for build in pending:
            instance = await sync_to_async(lambda: getattr(build, "image_instance", None))()
            if not instance or not instance.runner_ref:
                continue
            try:
                reused_active_task = False
                if not build.deleting_task_id:
                    task = None
                else:
                    existing_task = await sync_to_async(self.tasks.get_by_id)(
                        uuid.UUID(build.deleting_task_id)
                    )
                    if existing_task and existing_task.status in {
                        TaskStatus.PENDING,
                        TaskStatus.IN_PROGRESS,
                    }:
                        task = existing_task
                        reused_active_task = (
                            build.status == ImageBuildJob.Status.DELETING
                            and instance.status == ImageInstance.Status.DELETING
                        )
                    else:
                        task = None
                if task is None:
                    task_id = generate_uuid()
                    task = await sync_to_async(self.tasks.create)(
                        task_id=task_id,
                        runner=runner,
                        task_type=TaskType.DELETE_IMAGE,
                    )

                await sync_to_async(self._mark_definition_deleting_if_needed)(
                    build.image_definition_id
                )
                if not reused_active_task:
                    await sync_to_async(self.build_jobs.mark_deleting)(
                        build.id, deleting_task_id=str(task.id)
                    )
                    await sync_to_async(self.image_instances.mark_deleting)(
                        instance.id, deleting_task_id=str(task.id)
                    )
                await self._emit_to_runner(
                    runner,
                    "task:delete_image_artifact",
                    {
                        "task_id": str(task.id),
                        "image_instance_id": str(instance.id),
                        "runtime_type": build.image_definition.runtime_type,
                        "image_artifact_id": instance.runner_ref,
                    },
                )
                await sync_to_async(self.tasks.mark_in_progress)(task)
                dispatched.append(build)
            except Exception:
                logger.exception(
                    "Failed to dispatch pending build deletion %s for runner %s",
                    build.id, runner.id,
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
        credential_svc = CredentialSvc()
        image = await sync_to_async(self.image_instances.get_by_id)(image_artifact_id)
        if image is None:
            raise ValueError(f"Image artifact '{image_artifact_id}' not found")

        if image.status != "ready":
            raise ConflictError(f"Image artifact '{image_artifact_id}' is not ready")

        source_workspace = image.origin_workspace
        if source_workspace is not None:
            runner = source_workspace.runner
            runtime_type = source_workspace.runtime_type
            qemu_vcpus = source_workspace.qemu_vcpus
            qemu_memory_mb = source_workspace.qemu_memory_mb
            qemu_disk_size_gb = source_workspace.qemu_disk_size_gb
        elif image.build_job is not None:
            runner = image.build_job.runner
            runtime_type = image.build_job.image_definition.runtime_type
            qemu_vcpus = None
            qemu_memory_mb = None
            qemu_disk_size_gb = None
        else:
            raise ValueError(
                f"Image artifact '{image_artifact_id}' is missing its source runtime metadata"
            )

        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        if source_workspace is not None:
            self._ensure_workspace_available(source_workspace)

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
            await sync_to_async(credential_svc.assert_unique_workspace_credentials)(credentials)

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
