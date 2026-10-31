"""Transaction-scoped reads and writes for durable workspace recreation.

RecreateService owns reset/update admission, image selection and phase progression.
This repository keeps their persistence atomic under the runner-first lock shared
with agent admission, lifecycle callbacks and capture replacement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import transaction

from .image_lines import ImageLine, ImageLineRepository
from .locking import lock_runner
from .models import (
    CapturedImage,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from .phase_children import fence_unknown_child
from .repositories import ImageInstanceRepository, TaskRepository
from .workspace_operation_repository import REQUEST_TERMINAL_PHASES


class RecreateRepository:
    """Lock recreate records and persist decisions made by RecreateService."""

    @staticmethod
    @contextmanager
    def lock_workspace(workspace_id: uuid.UUID) -> Iterator[Workspace]:
        """Keep admission, target pinning and the first child in one transaction."""
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
    def lock_request(request_id: uuid.UUID) -> Iterator[WorkspaceRecreateRequest]:
        """Keep phase progression under the runner -> request lock order."""
        with transaction.atomic():
            runner_id = WorkspaceRecreateRequest.objects.values_list(
                "workspace__runner_id", flat=True
            ).get(pk=request_id)
            lock_runner(runner_id)
            yield WorkspaceRecreateRequest.objects.select_for_update(of=("self",)).get(
                pk=request_id
            )

    @staticmethod
    def lock_child(
        request: WorkspaceRecreateRequest,
    ) -> tuple[Workspace, Task, LifecycleCommand]:
        """Load progress in workspace -> child order within lock_request."""
        ws = (
            Workspace.objects.select_for_update(of=("self",))
            .select_related("runner", "base_image_instance")
            .get(pk=request.workspace_id)
        )
        child = Task.objects.select_for_update().get(pk=request.child_id)
        command = LifecycleCommand.objects.get(task=child)
        return ws, child, command

    @staticmethod
    def lock_image(runner_id: uuid.UUID, image_id: uuid.UUID) -> ImageInstance | None:
        """Lock a runner-scoped version and its captured-image line, if any."""
        image = (
            ImageInstance.objects.select_for_update(of=("self",))
            .select_related("captured_image", "build_job__image_definition")
            .filter(pk=image_id, runner_id=runner_id)
            .first()
        )
        if image is not None and image.captured_image_id:
            CapturedImage.objects.select_for_update().get(pk=image.captured_image_id)
        if image is not None:
            ImageInstanceRepository.populate_disk_floor(image)
        return image

    @staticmethod
    def source_line(ws: Workspace) -> ImageLine | None:
        """Load the pinned target's lineage, or the current base's when unpinned."""
        source = ws.pending_base_image_instance or ws.base_image_instance
        return ImageLineRepository.line_of(source)

    @staticmethod
    def create_request(
        ws: Workspace,
        *,
        reason: str,
        final_running: bool,
        requested_by: object | None = None,
    ) -> WorkspaceRecreateRequest:
        """Persist the initial reservation within the caller's transaction."""
        return WorkspaceRecreateRequest.objects.create(
            workspace=ws,
            reason=reason,
            phase="remove",
            final_running=final_running,
            requested_by=requested_by,
        )

    @staticmethod
    def create_child(
        request: WorkspaceRecreateRequest,
        ws: Workspace,
        phase: str,
        task_type: str,
        operation_payload: dict | None,
    ) -> Task:
        """Persist a service-selected child and its prepared lifecycle command."""
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
    def pending_ids() -> list[uuid.UUID]:
        """Read unfinished reservations in their existing recovery order."""
        return list(
            WorkspaceRecreateRequest.objects.exclude(
                phase__in=REQUEST_TERMINAL_PHASES
            ).values_list("id", flat=True)
        )

    @staticmethod
    def fence_child(
        request: WorkspaceRecreateRequest,
        ws: Workspace,
        child: Task,
        command: LifecycleCommand,
    ) -> tuple[bool, dict | None]:
        """Persist the shared lifecycle fence for an ambiguous child outcome."""
        return fence_unknown_child(request, ws, child, command)

    @staticmethod
    def save_request(request: WorkspaceRecreateRequest, fields: list[str]) -> None:
        """Persist service-selected reservation changes."""
        request.save(update_fields=fields)

    @staticmethod
    def save_workspace(ws: Workspace, fields: list[str]) -> None:
        """Persist service-selected workspace changes."""
        ws.save(update_fields=fields)

    @staticmethod
    def update_workspace(workspace_id: uuid.UUID, updates: dict) -> None:
        """Apply confirmed removal changes without mutating a caller's snapshot."""
        Workspace.objects.filter(pk=workspace_id).update(**updates)

    @staticmethod
    def refresh_workspace(ws: Workspace) -> None:
        """Reload changed fields and materialize inputs for command composition."""
        ws.refresh_from_db(
            from_queryset=Workspace.objects.select_related(
                "runner", "base_image_instance"
            )
        )
