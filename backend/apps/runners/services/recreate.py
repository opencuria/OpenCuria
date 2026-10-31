"""Workspace reset/update admission, image selection and durable phase progression.

Policy runs inside repository-owned transactions, preserving runner -> workspace
-> child lock order and atomic allocation, recovery and capture handoff.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import structlog

from common.exceptions import ConflictError, NotFoundError

from ..enums import TaskType, WorkspaceStatus
from ..image_lines import ImageLineRepository
from ..models import ImageInstance, Task, Workspace, WorkspaceRecreateRequest
from ..provisioning import disk_size_for, provisioning_command
from ..recreate_repository import RecreateRepository
from ..workspace_operation_repository import (
    REQUEST_TERMINAL_PHASES,
    WorkspaceOperationRepository,
)
from .workspace_capacity import WorkspaceCapacityService

logger = structlog.get_logger("apps.runners.recreate_repository")

RECREATABLE_STATUSES = {
    WorkspaceStatus.RUNNING,
    WorkspaceStatus.STOPPED,
    WorkspaceStatus.FAILED,
}


class RecreateService:
    """Own reset/update reservations and remove/create/stop orchestration."""

    @staticmethod
    def allocate(
        workspace_id: uuid.UUID,
        image_id: uuid.UUID,
        *,
        has_busy_agent: Callable[[uuid.UUID], bool],
        requested_by: object | None = None,
        qemu_disk_size_gb: int | None = None,
    ) -> tuple[Workspace, Task, WorkspaceRecreateRequest]:
        """Validate and reserve recreation atomically with the first command.

        ``has_busy_agent`` is evaluated under the runner lock shared with
        agent admission, before destroying or changing any workspace state.
        """
        with RecreateRepository.lock_workspace(workspace_id) as ws:
            RecreateService._ensure_idle(ws, has_busy_agent)
            target = RecreateService._lock_target(ws, image_id)
            reason = (
                WorkspaceRecreateRequest.Reason.RESET
                if target.id == ws.base_image_instance_id
                else WorkspaceRecreateRequest.Reason.UPDATE
            )
            qemu_disk_size_gb = disk_size_for(
                qemu_disk_size_gb
                if qemu_disk_size_gb is not None
                else ws.qemu_disk_size_gb,
                target,
            )
            if qemu_disk_size_gb is not None:
                ws.qemu_disk_size_gb = qemu_disk_size_gb
                RecreateRepository.save_workspace(
                    ws, ["qemu_disk_size_gb", "updated_at"]
                )
            request, task = RecreateService.begin(
                ws,
                target,
                reason=reason,
                final_running=ws.status != WorkspaceStatus.STOPPED,
                requested_by=requested_by,
            )
            RecreateRepository.refresh_workspace(ws)
            return ws, task, request

    @staticmethod
    def _ensure_idle(
        ws: Workspace, has_busy_agent: Callable[[uuid.UUID], bool]
    ) -> None:
        """Reject every state in which destroying the runtime is not well defined."""
        approvals = WorkspaceOperationRepository.forced_deletion_approvals(
            ws.runner.organization_id
        )
        for approval in approvals:
            if str(ws.id) in [w["id"] for w in approval.get("workspaces", [])]:
                raise ConflictError("Workspace is reserved by approved image deletion")
        if ws.status not in RECREATABLE_STATUSES:
            raise ConflictError(
                "Only running, stopped or failed workspaces can be reset"
            )
        if (
            ws.active_operation
            or ws.current_task_id
            or WorkspaceOperationRepository.capture_active(ws.id)
            or WorkspaceOperationRepository.recreate_active(ws.id)
        ):
            raise ConflictError("Workspace has an unresolved lifecycle operation")
        if WorkspaceOperationRepository.credentials_synchronizing(ws.id):
            raise ConflictError(
                "Workspace credentials are synchronizing. Try again in a moment."
            )
        if has_busy_agent(ws.id):
            raise ConflictError(
                "An agent is active or waiting for an answer or approval. "
                "Finish or stop it before resetting this workspace."
            )

    @staticmethod
    def _lock_target(ws: Workspace, image_id: uuid.UUID) -> ImageInstance:
        """Only the workspace's own version or the latest of its line are allowed."""
        image = RecreateRepository.lock_image(ws.runner_id, image_id)
        if image is None:
            raise NotFoundError("Image version", str(image_id))
        own = {ws.base_image_instance_id, ws.pending_base_image_instance_id} - {None}
        if image.id not in own:
            source_line = RecreateRepository.source_line(ws)
            if source_line is None or source_line != ImageLineRepository.line_of(image):
                raise ConflictError(
                    "A workspace can only move to the latest version of its image"
                )
            latest = ImageLineRepository.latest(source_line)
            if latest is None or latest.id != image.id:
                raise ConflictError(
                    "A newer image version is available; review the update again"
                )
        if image.status != "ready" or not image.runner_ref:
            raise ConflictError(
                "This image version is being deleted or unavailable. "
                "Update to the latest version or delete the workspace."
            )
        return image

    @staticmethod
    def begin(
        ws: Workspace,
        target: ImageInstance,
        *,
        reason: str,
        final_running: bool,
        requested_by: object | None = None,
    ) -> tuple[WorkspaceRecreateRequest, Task]:
        """Reserve removal for an admitted workspace; caller holds the locks.

        CaptureService uses the same workflow inside its handoff transaction.
        """
        disk_size_gb = disk_size_for(ws.qemu_disk_size_gb, target)
        WorkspaceCapacityService.ensure(
            ws.runner_id,
            vcpus=ws.qemu_vcpus,
            memory_mb=ws.qemu_memory_mb,
            disk_size_gb=disk_size_gb,
            exclude_workspace_id=ws.id,
        )
        ws.qemu_disk_size_gb = disk_size_gb
        ws.pending_base_image_instance = target
        RecreateRepository.save_workspace(
            ws, ["qemu_disk_size_gb", "pending_base_image_instance", "updated_at"]
        )
        request = RecreateRepository.create_request(
            ws, reason=reason, final_running=final_running, requested_by=requested_by
        )
        task = RecreateService._child(request, ws, "remove")
        return request, task

    @staticmethod
    def _child(request: WorkspaceRecreateRequest, ws: Workspace, phase: str) -> Task:
        operation_payload = None
        if phase == "remove":
            task_type = TaskType.REMOVE_WORKSPACE
        elif phase == "stop":
            task_type = TaskType.STOP_WORKSPACE
        else:
            command = provisioning_command(
                workspace_id=ws.id,
                workspace_name=ws.name,
                image=ws.base_image_instance,
                repos=ws.repos or [],
                qemu_vcpus=ws.qemu_vcpus,
                qemu_memory_mb=ws.qemu_memory_mb,
                qemu_disk_size_gb=ws.qemu_disk_size_gb,
            )
            task_type = command.task_type
            operation_payload = command.payload
        return RecreateRepository.create_child(
            request, ws, phase, task_type, operation_payload
        )

    @staticmethod
    def removed(workspace: Workspace) -> None:
        """The old runtime is gone: the workspace is now based on the target."""
        updates = {"status": WorkspaceStatus.CREATING, "credentials_present": False}
        if workspace.pending_base_image_instance_id:
            updates.update(
                base_image_instance_id=workspace.pending_base_image_instance_id,
                pending_base_image_instance=None,
            )
        RecreateRepository.update_workspace(workspace.id, updates)

    @staticmethod
    def tick() -> list[dict]:
        """Allocate the next child after terminal evidence of the previous one."""
        ids = RecreateRepository.pending_ids()
        finished = []
        for request_id in ids:
            try:
                result = RecreateService.advance(request_id)
                if result:
                    finished.append(result)
            except Exception:
                logger.exception("recreate_advance_failed", request_id=str(request_id))
        return finished

    @staticmethod
    def advance(request_id: uuid.UUID) -> dict | None:
        """Progress one child and return a final notification after commit."""
        with RecreateRepository.lock_request(request_id) as request:
            if request.phase in REQUEST_TERMINAL_PHASES:
                return None
            ws, child, command = RecreateRepository.lock_child(request)
            if child.status not in ["completed", "failed"]:
                return None
            fenced, notification = RecreateRepository.fence_child(
                request, ws, child, command
            )
            if fenced:
                return notification
            if request.phase == "remove":
                if child.status == "completed":
                    if ws.pending_base_image_instance_id:
                        RecreateService.removed(ws)
                        RecreateRepository.refresh_workspace(ws)
                    RecreateService._child(request, ws, "create")
                    return None
                request.phase = "failed"
                request.diagnostic = (
                    "Could not remove the previous workspace: "
                    f"{child.error}. Retry the reset or delete the workspace."
                )
                ws.status = WorkspaceStatus.FAILED
            elif request.phase == "create":
                if child.status == "completed":
                    if not request.final_running:
                        RecreateService._child(request, ws, "stop")
                        return None
                    request.phase = "completed"
                else:
                    request.phase = "failed"
                    request.diagnostic = (
                        "Could not create the workspace from the image: "
                        f"{child.error}. Retry the reset or delete the workspace."
                    )
                    ws.status = WorkspaceStatus.FAILED
            elif request.phase == "stop":
                request.phase = "completed"
                if child.status == "failed":
                    request.diagnostic = (
                        "The workspace runs on the new image version, but stopping "
                        f"it failed: {child.error}"
                    )
            RecreateRepository.save_request(
                request, ["phase", "diagnostic", "updated_at"]
            )
            ws.active_operation = None
            RecreateRepository.save_workspace(
                ws, ["status", "active_operation", "updated_at"]
            )
            return {
                "workspace_id": str(ws.id),
                "phase": request.phase,
                "status": ws.status,
                "diagnostic": request.diagnostic,
            }
