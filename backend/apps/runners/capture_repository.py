"""Transaction-scoped reads and writes for workspace image captures.

CaptureService owns admission, replacement policy and phase progression. This
repository retains the runner-first lock order and atomic persistence boundary
for each allocation or progression, including a replacement handoff.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import transaction

from .image_lines import ImageLineRepository
from .locking import lock_runner
from .models import (
    CapturedImage,
    CaptureRequest,
    ImageDeletionRequest,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
)
from .operations import OperationRepository
from .phase_children import fence_unknown_child
from .repositories import TaskRepository, WorkspaceProcessRepository


class CaptureRepository:
    """Lock capture records and persist decisions made by the capture service."""

    @staticmethod
    @contextmanager
    def lock_workspace(workspace_id: uuid.UUID) -> Iterator[Workspace]:
        """Keep admission checks and the initial child in one transaction."""
        with transaction.atomic():
            runner_id = Workspace.objects.values_list("runner_id", flat=True).get(
                pk=workspace_id
            )
            lock_runner(runner_id)
            yield (
                Workspace.objects.select_for_update(of=("self",))
                .select_related("runner")
                .get(pk=workspace_id)
            )

    @staticmethod
    @contextmanager
    def lock_request(request_id: uuid.UUID) -> Iterator[CaptureRequest]:
        """Keep progression and replacement under the existing runner lock."""
        with transaction.atomic():
            runner_id = CaptureRequest.objects.values_list(
                "workspace__runner_id", flat=True
            ).get(pk=request_id)
            lock_runner(runner_id)
            yield (
                CaptureRequest.objects.select_for_update(of=("self",))
                .select_related("image", "workspace__runner")
                .get(pk=request_id)
            )

    @staticmethod
    def lock_child(request: CaptureRequest) -> tuple[Workspace, Task, LifecycleCommand]:
        """Load progress in workspace -> child order within lock_request."""
        ws = (
            Workspace.objects.select_for_update(of=("self",))
            .select_related("runner")
            .get(pk=request.workspace_id)
        )
        child = Task.objects.select_for_update().get(pk=request.child_id)
        command = LifecycleCommand.objects.get(task=child)
        return ws, child, command

    @staticmethod
    def forced_deletion_approvals(organization_id: uuid.UUID) -> list[dict]:
        """Load live forced-deletion approvals for admission checks."""
        return list(
            ImageDeletionRequest.objects.filter(
                organization_id=organization_id, mode="force"
            )
            .exclude(phase__in=["completed", "cancelled"])
            .values_list("approval", flat=True)
        )

    @staticmethod
    def credentials_synchronizing(workspace_id: uuid.UUID) -> bool:
        """Whether credential injection is pending or executing."""
        return Task.objects.filter(
            workspace_id=workspace_id,
            type="inject_credentials",
            status__in=["pending", "in_progress"],
        ).exists()

    @staticmethod
    def image_unavailable(image_id: uuid.UUID) -> bool:
        """Whether an existing source image is no longer ready."""
        return (
            ImageInstance.objects.filter(pk=image_id).exclude(status="ready").exists()
        )

    @staticmethod
    def create_line(ws: Workspace, name: str) -> CapturedImage:
        """Persist a new captured-image line for the workspace owner."""
        return CapturedImage.objects.create(
            organization_id=ws.runner.organization_id,
            runner=ws.runner,
            created_by=ws.created_by,
            name=name,
        )

    @staticmethod
    def lock_line(
        line_id: uuid.UUID, organization_id: uuid.UUID
    ) -> CapturedImage | None:
        """Read an organization-scoped line under its version-allocation lock."""
        return (
            CapturedImage.objects.select_for_update()
            .filter(pk=line_id, organization_id=organization_id)
            .first()
        )

    @staticmethod
    def version_capturing(line: CapturedImage) -> bool:
        """Whether a line already has a capturing version."""
        return ImageInstance.objects.filter(
            captured_image=line, status="capturing"
        ).exists()

    @staticmethod
    def create_request(
        ws: Workspace,
        line: CapturedImage,
        *,
        phase: str,
        prior_running: bool,
        message: str,
    ) -> CaptureRequest:
        """Reserve an image version with an immutable process-definition snapshot."""
        image = ImageInstance.objects.create(
            runner=ws.runner,
            origin_type="workspace_capture",
            origin_workspace=ws,
            created_by=ws.created_by,
            name=line.name,
            captured_image=line,
            generation=ImageLineRepository.next_captured_version(line.id),
            message=message,
            min_disk_size_gb=ws.qemu_disk_size_gb,
            process_definitions=WorkspaceProcessRepository.snapshot_definitions(ws.id),
            status="capturing",
        )
        return CaptureRequest.objects.create(
            workspace=ws, image=image, phase=phase, prior_running=prior_running
        )

    @staticmethod
    def create_child(
        request: CaptureRequest, ws: Workspace, phase: str, task_type: str
    ) -> Task:
        """Persist a correlated child and its prepared lifecycle command atomically."""
        task = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=ws.runner,
            task_type=task_type,
            workspace=ws,
            capture_request_id=request.id,
        )
        payload = {"task_id": str(task.id), "workspace_id": str(ws.id)}
        if phase == "capture":
            payload.update(
                image_instance_id=str(request.image_id), name=request.image.name
            )
            ImageInstance.objects.filter(pk=request.image_id).update(creating_task=task)
        OperationRepository.prepare(str(task.id), "task:" + task_type, payload)
        request.child = task
        request.phase = phase
        request.save(update_fields=["child", "phase"])
        return task

    @staticmethod
    def pending_ids() -> list[uuid.UUID]:
        """Read unfinished capture reservations for recovery."""
        return list(
            CaptureRequest.objects.exclude(
                phase__in=["completed", "failed"]
            ).values_list("id", flat=True)
        )

    @staticmethod
    def fence_child(
        request: CaptureRequest, ws: Workspace, child: Task, command: LifecycleCommand
    ) -> tuple[bool, dict | None]:
        """Persist the shared lifecycle fence for an ambiguous child outcome."""
        return fence_unknown_child(request, ws, child, command)

    @staticmethod
    def fail_image(image_id: uuid.UUID) -> None:
        """Persist failure of an unpublished captured version."""
        ImageInstance.objects.filter(pk=image_id).update(status="failed")

    @staticmethod
    def save_request(request: CaptureRequest, fields: list[str]) -> None:
        """Persist the service-selected phase and diagnostic changes."""
        request.save(update_fields=fields)

    @staticmethod
    def clear_operation(ws: Workspace) -> None:
        """Release the source's active capture operation."""
        ws.active_operation = None
        ws.save(update_fields=["active_operation", "updated_at"])

    @staticmethod
    def lock_image(image_id: uuid.UUID) -> ImageInstance:
        """Lock the published version before checking replacement eligibility."""
        return ImageInstance.objects.select_for_update().get(pk=image_id)

    @staticmethod
    def image(image_id: uuid.UUID) -> ImageInstance:
        """Reload a capture version for the replacement reservation."""
        return ImageInstance.objects.get(pk=image_id)

    @staticmethod
    def clear_repos(ws: Workspace) -> None:
        """Persist that cloned repositories now belong to the captured image."""
        ws.repos = []
        ws.save(update_fields=["repos", "updated_at"])

    @staticmethod
    def refresh_workspace(ws: Workspace) -> None:
        """Reload workspace fields changed by child allocation."""
        ws.refresh_from_db()
