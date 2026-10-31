"""Loaded operation state and transaction-scoped disposition persistence."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .image_assignment_repository import ImageAssignmentRepository
from .locking import lock_runner
from .models import (
    CaptureRequest,
    ImageBuildJob,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from .operations import OperationRepository
from .repositories import TaskRepository


@dataclass(frozen=True)
class DispositionState:
    """Operation snapshot with an eagerly loaded runner for transport calls."""

    operation_id: uuid.UUID
    runner: Runner
    identity: dict[str, Any]
    task_type: str
    status: str
    phase: str
    diagnostic: str
    retry_count: int
    workspace_id: uuid.UUID | None
    workspace_owner_id: int | None
    workspace_status: str | None
    current_task_id: uuid.UUID | None
    legacy_credentials: bool = False


def _snapshot(command: LifecycleCommand) -> DispositionState:
    """Copy loaded operation/workspace fields without exposing lazy relations."""
    task = command.task
    workspace = task.workspace if task.workspace_id else None
    return DispositionState(
        operation_id=task.id,
        runner=task.runner,
        identity=OperationRepository.envelope(command, {}),
        task_type=task.type,
        status=task.status,
        phase=command.phase,
        diagnostic=task.error,
        retry_count=command.payload.get("_retry_count", 0),
        workspace_id=task.workspace_id,
        workspace_owner_id=workspace.created_by_id if workspace else None,
        workspace_status=workspace.status if workspace else None,
        current_task_id=workspace.current_task_id if workspace else None,
        legacy_credentials=command.payload.get("_legacy_credential_sync") is True,
    )


class DispositionRepository:
    """Hold runner → workspace → task locks and persist service-selected changes."""

    def __init__(self, command: LifecycleCommand) -> None:
        self._command = command
        self._task = command.task
        self._workspace = self._task.workspace if self._task.workspace_id else None

    @staticmethod
    def get(
        organization_id: uuid.UUID, operation_id: uuid.UUID
    ) -> DispositionState | None:
        """Read a fully loaded operation in the requested organization."""
        row = (
            LifecycleCommand.objects.select_related("task__runner", "task__workspace")
            .filter(task_id=operation_id, task__runner__organization_id=organization_id)
            .first()
        )
        return _snapshot(row) if row is not None else None

    @staticmethod
    def for_runner(runner_id: str, operation_id: str) -> DispositionState | None:
        """Load an authenticated runner's operation without user authorization."""
        try:
            operation_uuid = uuid.UUID(str(operation_id))
            runner_uuid = uuid.UUID(str(runner_id))
            row = (
                LifecycleCommand.objects.select_related(
                    "task__runner", "task__workspace"
                )
                .filter(task_id=operation_uuid, task__runner_id=runner_uuid)
                .first()
            )
        except (ValueError, TypeError):
            return None
        return _snapshot(row) if row is not None else None

    @staticmethod
    @contextmanager
    def locked(state: DispositionState) -> Iterator[DispositionRepository]:
        """Keep the existing allocation/callback lock order through all writes.

        Policy validation and retry allocation run inside this transaction so
        stale state is rechecked and failures roll back the whole disposition.
        """
        with transaction.atomic():
            runner = lock_runner(state.runner.id)
            workspace = None
            if state.workspace_id:
                workspace = Workspace.objects.select_for_update().get(
                    pk=state.workspace_id
                )
            list(
                ImageBuildJob.objects.select_for_update().filter(
                    build_task_id=state.operation_id
                )
            )
            list(
                ImageInstance.objects.select_for_update().filter(
                    Q(creating_task_id=state.operation_id)
                    | Q(deleting_task_id=state.operation_id)
                )
            )
            task = Task.objects.select_for_update().get(pk=state.operation_id)
            command = LifecycleCommand.objects.select_for_update().get(pk=task.id)
            task.runner = runner
            task.workspace = workspace
            command.task = task
            yield DispositionRepository(command)

    def state(self) -> DispositionState:
        """Read the current locked operation and workspace fence."""
        return _snapshot(self._command)

    def fail_task(self, error: str) -> None:
        """Persist the terminal task outcome selected by the service."""
        task = self._task
        task.status = "failed"
        task.error = error
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "error", "completed_at"])

    def interrupt_children(
        self, capture_diagnostic: str, recreate_diagnostic: str
    ) -> None:
        """Close child reservations while retaining finished/deleting images."""
        task = self._task
        captures = CaptureRequest.objects.filter(child=task)
        capture_image_ids = list(captures.values_list("image_id", flat=True))
        captures.update(
            phase="failed",
            resume_suppressed=True,
            diagnostic=capture_diagnostic,
        )
        WorkspaceRecreateRequest.objects.filter(child=task).exclude(
            phase__in=["completed", "failed"]
        ).update(phase="failed", diagnostic=recreate_diagnostic)
        # The reservation precedes the capture task: an interrupted stop
        # still owns an unfinished image with no creating_task yet.
        ImageInstance.objects.filter(
            Q(creating_task=task) | Q(pk__in=capture_image_ids, status="capturing")
        ).exclude(
            status__in=[
                "ready",
                "retired",
                "deleted",
                "pending_deletion",
                "deleting",
                "delete_failed",
            ]
        ).update(status="failed", updated_at=timezone.now())
        ImageInstance.objects.filter(deleting_task=task, status="deleting").update(
            status="delete_failed",
            delete_last_error=task.error,
            updated_at=timezone.now(),
        )
        for job in ImageBuildJob.objects.select_for_update().filter(build_task=task):
            ImageAssignmentRepository.reconcile(job)

    def has_recreate_request(self) -> bool:
        """Read whether the locked child belongs to a workspace reset."""
        return WorkspaceRecreateRequest.objects.filter(child=self._task).exists()

    def release(
        self,
        workspace_status: str | None,
        credentials_present: bool | None = None,
        observed_status: str | None = None,
    ) -> None:
        """Mark the command disposed and release its workspace reservation."""
        self._command.phase = "disposed"
        self._command.payload = {
            **self._command.payload,
            "_observed_workspace_status": observed_status or "unknown",
        }
        self._command.save(update_fields=["phase", "payload"])
        if self._workspace is not None:
            workspace = self._workspace
            workspace.current_task = None
            workspace.active_operation = None
            workspace.status = workspace_status
            fields = ["current_task", "active_operation", "status"]
            if isinstance(credentials_present, bool):
                workspace.credentials_present = credentials_present
                fields.append("credentials_present")
            workspace.save(update_fields=fields)

    def create_retry(self, retry_count: int) -> uuid.UUID:
        """Allocate a new task identity inside the disposition transaction."""
        new = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=self._task.runner,
            task_type=self._task.type,
            workspace=self._workspace,
            operation_payload={**self._command.payload, "_retry_count": retry_count},
        )
        return new.id
