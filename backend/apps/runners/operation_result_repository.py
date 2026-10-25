"""Transaction-scoped persistence for lifecycle result reconciliation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .locking import lock_runner
from .models import ImageBuildJob, ImageInstance, LifecycleCommand, Task, Workspace
from .operations import OperationRepository
from .repositories import TaskRepository


@dataclass(frozen=True)
class OperationResultState:
    """Loaded identity and journal state, without lazy ORM access in services."""

    task_id: uuid.UUID
    runner_id: uuid.UUID
    task_type: str
    status: str
    phase: str
    attempt: int
    target: str
    workspace_id: uuid.UUID | None
    current_task_id: uuid.UUID | None


class OperationResultRepository:
    """Hold result locks and persist changes selected by the result service."""

    def __init__(self, task: Task, command: LifecycleCommand) -> None:
        self._task = task
        self._command = command

    @staticmethod
    @contextmanager
    def locked(
        runner_id: str, operation_id: str
    ) -> Iterator[OperationResultRepository | None]:
        """Keep runner → workspace → image → task locks through callback writes.

        The transaction includes callback writes; callback errors roll it back,
        including any notifications queued with ``on_commit``.
        """
        with transaction.atomic():
            lock_runner(runner_id)
            identity = (
                Task.objects.filter(id=operation_id).values("workspace_id").first()
            )
            if identity and identity["workspace_id"]:
                Workspace.objects.select_for_update().get(pk=identity["workspace_id"])
            list(
                ImageBuildJob.objects.select_for_update().filter(
                    build_task_id=operation_id
                )
            )
            list(
                ImageInstance.objects.select_for_update().filter(
                    Q(creating_task_id=operation_id) | Q(deleting_task_id=operation_id)
                )
            )
            Task.objects.select_for_update().filter(pk=operation_id).first()
            try:
                command = (
                    LifecycleCommand.objects.select_for_update(of=("self",))
                    .select_related("task__workspace")
                    .get(task_id=operation_id)
                )
            except (LifecycleCommand.DoesNotExist, ValueError):
                yield None
                return
            task = Task.objects.select_for_update().get(pk=command.task_id)
            yield OperationResultRepository(task, command)

    def state(self) -> OperationResultState:
        """Read the identity and workspace fence while the result locks are held."""
        task, command = self._task, self._command
        return OperationResultState(
            task_id=task.id,
            runner_id=task.runner_id,
            task_type=task.type,
            status=task.status,
            phase=command.phase,
            attempt=command.attempt,
            target=command.target,
            workspace_id=task.workspace_id,
            current_task_id=task.workspace.current_task_id
            if task.workspace_id
            else None,
        )

    def intervene(self, reason: str) -> None:
        """Persist an unresolved outcome using the shared intervention writer."""
        OperationRepository.intervene(self._task, reason)

    def reopen(self, creating_image_status: str) -> None:
        """Restore the task and image rows before reconciling a late result."""
        task = self._task
        task.status = "in_progress"
        task.error = ""
        task.completed_at = None
        task.save(update_fields=["status", "error", "completed_at"])
        ImageInstance.objects.filter(creating_task=task, status="failed").update(
            status=creating_image_status
        )
        ImageInstance.objects.filter(deleting_task=task, status="delete_failed").update(
            status="deleting"
        )

    def mark_received(self) -> None:
        """Record receipt before invoking the type-specific lifecycle callback."""
        self._command.heartbeat_at = timezone.now()
        self._command.phase = "result"
        self._command.save(update_fields=["heartbeat_at", "phase"])

    def outcome(self) -> tuple[str, str]:
        """Reload the callback's task outcome without exposing a mutable model."""
        self._task.refresh_from_db()
        return self._task.status, self._task.error

    def retain_intervention_fence(self) -> None:
        """Reserve the task identity when a callback still needs intervention."""
        self._command.phase = "intervention"
        self._command.save(update_fields=["phase"])
        if self._task.workspace_id:
            Workspace.objects.filter(pk=self._task.workspace_id).update(
                current_task=self._task, active_operation=None
            )

    def release_workspace(self) -> None:
        """Release only this child identity, preserving capture/recreate ownership."""
        TaskRepository.release_workspace(self._task)
