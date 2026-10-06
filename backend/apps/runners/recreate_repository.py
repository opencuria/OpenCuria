"""Durable workspace recreate: remove the runtime, provision a version, restore state.

Reset, update and the replacement after a capture share this one mechanism.
The workspace row (and with it chats, credentials, plugins and schedules) is
kept; only the runner runtime and disk are recreated under the same id.
"""

from __future__ import annotations

import uuid

from django.db import transaction

from common.exceptions import ConflictError, NotFoundError

from .enums import TaskType, WorkspaceStatus
from .image_lines import ImageLineRepository
from .locking import lock_runner
from .models import (
    CapturedImage,
    ImageDeletionRequest,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from .phase_children import TERMINAL_PHASES, fence_unknown_child
from .provisioning import provisioning_command
from .repositories import TaskRepository

RECREATABLE_STATUSES = {
    WorkspaceStatus.RUNNING,
    WorkspaceStatus.STOPPED,
    WorkspaceStatus.FAILED,
}
OPERATION_BY_REASON = {
    WorkspaceRecreateRequest.Reason.CAPTURE: "capturing_image",
    WorkspaceRecreateRequest.Reason.RESET: "resetting",
    WorkspaceRecreateRequest.Reason.UPDATE: "updating",
}


class RecreateRepository:
    """Own recreate reservations, child allocation and phase progression."""

    @staticmethod
    def live(workspace_id: uuid.UUID | str) -> WorkspaceRecreateRequest | None:
        """The unfinished recreate request of a workspace, if any."""
        return (
            WorkspaceRecreateRequest.objects.filter(workspace_id=workspace_id)
            .exclude(phase__in=TERMINAL_PHASES)
            .first()
        )

    @staticmethod
    def active(workspace_id: uuid.UUID | str) -> bool:
        """Whether a recreate still owns the workspace between children."""
        return RecreateRepository.live(workspace_id) is not None

    @staticmethod
    def operation(workspace_id: uuid.UUID | str, fallback: str | None) -> str | None:
        """Project the whole recreate as one operation, keeping interventions."""
        request = RecreateRepository.live(workspace_id)
        if request is None:
            return fallback
        if LifecycleCommand.objects.filter(
            task__workspace_id=workspace_id,
            task_id=Workspace.objects.filter(pk=workspace_id).values("current_task_id")[
                :1
            ],
            phase="intervention",
        ).exists():
            return None
        return OPERATION_BY_REASON[request.reason]

    @staticmethod
    def owns(task: Task) -> bool:
        """Whether a task is the current child of an unfinished recreate."""
        return (
            WorkspaceRecreateRequest.objects.filter(child=task)
            .exclude(phase__in=TERMINAL_PHASES)
            .exists()
        )

    @staticmethod
    def allocate(
        workspace_id: uuid.UUID,
        image_id: uuid.UUID,
        *,
        requested_by=None,
        qemu_disk_size_gb: int | None = None,
    ) -> tuple[Workspace, Task, WorkspaceRecreateRequest]:
        """Validate everything up front, then reserve the remove child atomically."""
        with transaction.atomic():
            runner_id = Workspace.objects.values_list("runner_id", flat=True).get(
                pk=workspace_id
            )
            lock_runner(runner_id)
            ws = (
                Workspace.objects.select_for_update(of=("self",))
                .select_related("runner")
                .get(pk=workspace_id)
            )
            RecreateRepository._ensure_idle(ws)
            target = RecreateRepository._lock_target(ws, image_id)
            reason = (
                WorkspaceRecreateRequest.Reason.RESET
                if target.id == ws.base_image_instance_id
                else WorkspaceRecreateRequest.Reason.UPDATE
            )
            if qemu_disk_size_gb is not None:
                ws.qemu_disk_size_gb = qemu_disk_size_gb
                ws.save(update_fields=["qemu_disk_size_gb", "updated_at"])
            request, task = RecreateRepository.begin(
                ws,
                target,
                reason=reason,
                final_running=ws.status != WorkspaceStatus.STOPPED,
                requested_by=requested_by,
            )
            ws.refresh_from_db()
            return ws, task, request

    @staticmethod
    def _ensure_idle(ws: Workspace) -> None:
        """Reject every state in which destroying the runtime is not well defined."""
        for deletion in ImageDeletionRequest.objects.filter(
            organization_id=ws.runner.organization_id, mode="force"
        ).exclude(phase__in=["completed", "cancelled"]):
            if str(ws.id) in [w["id"] for w in deletion.approval.get("workspaces", [])]:
                raise ConflictError("Workspace is reserved by approved image deletion")
        if ws.status not in RECREATABLE_STATUSES:
            raise ConflictError(
                "Only running, stopped or failed workspaces can be reset"
            )
        from .capture_repository import CaptureRepository

        if (
            ws.active_operation
            or ws.current_task_id
            or CaptureRepository.active(ws.id)
            or RecreateRepository.active(ws.id)
        ):
            raise ConflictError("Workspace has an unresolved lifecycle operation")
        if Task.objects.filter(
            workspace=ws,
            type="inject_credentials",
            status__in=["pending", "in_progress"],
        ).exists():
            raise ConflictError(
                "Workspace credentials are synchronizing. Try again in a moment."
            )
        from apps.harness.models import HarnessSession

        if HarnessSession.objects.filter(workspace=ws, status="busy").exists():
            raise ConflictError(
                "An agent is active or waiting for an answer or approval. "
                "Finish or stop it before resetting this workspace."
            )

    @staticmethod
    def _lock_target(ws: Workspace, image_id: uuid.UUID) -> ImageInstance:
        """Only the workspace's own version or the latest of its line are allowed."""
        image = (
            ImageInstance.objects.select_for_update()
            .select_related("captured_image", "build_job__image_definition")
            .filter(pk=image_id, runner_id=ws.runner_id)
            .first()
        )
        if image is None:
            raise NotFoundError("Image version", str(image_id))
        if image.captured_image_id:
            CapturedImage.objects.select_for_update().get(pk=image.captured_image_id)
        own = {ws.base_image_instance_id, ws.pending_base_image_instance_id} - {None}
        if image.id not in own:
            source = ws.pending_base_image_instance or ws.base_image_instance
            source_line = ImageLineRepository.line_of(source)
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
        if image.runtime_type != ws.runtime_type:
            raise ConflictError("Image version runtime does not match the workspace")
        return image

    @staticmethod
    def begin(
        ws: Workspace,
        target: ImageInstance,
        *,
        reason: str,
        final_running: bool,
        requested_by=None,
    ) -> tuple[WorkspaceRecreateRequest, Task]:
        """Pin the target and allocate the remove child; caller holds the locks."""
        ws.pending_base_image_instance = target
        ws.save(update_fields=["pending_base_image_instance", "updated_at"])
        request = WorkspaceRecreateRequest.objects.create(
            workspace=ws,
            reason=reason,
            phase="remove",
            final_running=final_running,
            requested_by=requested_by,
        )
        task = RecreateRepository._child(request, ws, "remove")
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
                runtime_type=ws.runtime_type,
                repos=ws.repos or [],
                qemu_vcpus=ws.qemu_vcpus,
                qemu_memory_mb=ws.qemu_memory_mb,
                qemu_disk_size_gb=ws.qemu_disk_size_gb,
            )
            task_type = command.task_type
            operation_payload = command.payload
        task = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=ws.runner,
            task_type=task_type,
            workspace=ws,
            operation_payload=operation_payload,
            recreate_request_id=request.id,
        )
        request.child = task
        request.phase = phase
        request.save(update_fields=["child", "phase", "updated_at"])
        return task

    @staticmethod
    def removed(workspace: Workspace) -> None:
        """The old runtime is gone: the workspace is now based on the target."""
        updates = {"status": WorkspaceStatus.CREATING, "credentials_present": False}
        if workspace.pending_base_image_instance_id:
            updates.update(
                base_image_instance_id=workspace.pending_base_image_instance_id,
                pending_base_image_instance_id=None,
            )
        Workspace.objects.filter(pk=workspace.pk).update(**updates)

    @staticmethod
    def tick() -> list[dict]:
        """Allocate the next child after terminal evidence of the previous one."""
        ids = list(
            WorkspaceRecreateRequest.objects.exclude(
                phase__in=TERMINAL_PHASES
            ).values_list("id", flat=True)
        )
        finished = []
        for request_id in ids:
            try:
                result = RecreateRepository.advance(request_id)
                if result:
                    finished.append(result)
            except Exception:
                import structlog

                structlog.get_logger(__name__).exception(
                    "recreate_advance_failed", request_id=str(request_id)
                )
        return finished

    @staticmethod
    def advance(request_id: uuid.UUID) -> dict | None:
        """Progress one child and return a final notification after commit."""
        with transaction.atomic():
            runner_id = WorkspaceRecreateRequest.objects.values_list(
                "workspace__runner_id", flat=True
            ).get(pk=request_id)
            lock_runner(runner_id)
            request = WorkspaceRecreateRequest.objects.select_for_update(
                of=("self",)
            ).get(pk=request_id)
            if request.phase in TERMINAL_PHASES:
                return None
            ws = (
                Workspace.objects.select_for_update(of=("self",))
                .select_related("runner", "base_image_instance")
                .get(pk=request.workspace_id)
            )
            child = Task.objects.select_for_update().get(pk=request.child_id)
            command = LifecycleCommand.objects.get(task=child)
            if child.status not in ["completed", "failed"]:
                return None
            fenced, notification = fence_unknown_child(request, ws, child, command)
            if fenced:
                return notification
            if request.phase == "remove":
                if child.status == "completed":
                    if ws.pending_base_image_instance_id:
                        RecreateRepository.removed(ws)
                        ws.refresh_from_db()
                    RecreateRepository._child(request, ws, "create")
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
                        RecreateRepository._child(request, ws, "stop")
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
            request.save(update_fields=["phase", "diagnostic", "updated_at"])
            ws.active_operation = None
            ws.save(update_fields=["status", "active_operation", "updated_at"])
            return {
                "workspace_id": str(ws.id),
                "phase": request.phase,
                "status": ws.status,
                "diagnostic": request.diagnostic,
            }
