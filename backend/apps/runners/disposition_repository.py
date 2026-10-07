"""Authorized operation inspection and transactional operator disposition."""

import uuid
from typing import Any

from django.db import transaction
from django.db.models import Q

from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .locking import lock_runner
from .models import LifecycleCommand, Task, Workspace
from .repositories import TaskRepository


class DispositionRepository:
    """The runner boundary serializes disposition with new allocation/callbacks."""

    @staticmethod
    def authorized(
        user: Any,
        organization_id: uuid.UUID,
        operation_id: uuid.UUID,
        *,
        admin: bool = False,
    ) -> LifecycleCommand:
        """Owners inspect workspace operations; org admins inspect every operation."""
        OrganizationService().require_membership(user, organization_id)
        row = (
            LifecycleCommand.objects.select_related("task__runner", "task__workspace")
            .filter(task_id=operation_id, task__runner__organization_id=organization_id)
            .first()
        )
        if row is None:
            raise NotFoundError("Operation", str(operation_id))
        if (
            admin
            or not row.task.workspace_id
            or row.task.workspace.created_by_id != user.id
        ):
            OrganizationService().require_admin(user, organization_id)
        return row

    @staticmethod
    def summary(row: LifecycleCommand) -> dict:
        """Hide recipes, repository URLs and credential associations."""
        return {
            "operation_id": str(row.task_id),
            "task_type": row.task.type,
            "status": row.task.status,
            "phase": row.phase,
            "runner_id": str(row.task.runner_id),
            "target": row.target,
            "attempt": row.attempt,
            "diagnostic": row.task.error,
            "intervention_required": row.phase == "intervention",
        }

    @staticmethod
    def permitted_actions(row, evidence: dict, user, organization_id) -> list[str]:
        """Presentation-only eligibility from current inspection; POST revalidates."""
        if row.task.status == "completed" or row.phase == "disposed":
            return []
        terminal = evidence.get("status") == "terminal"
        known = evidence.get("outcome_known")
        finished = evidence.get("execution_finished")
        actions = ["reconcile"] if terminal and known and finished else []
        identity = evidence.get("identity", {})
        exact = all(
            identity.get(k) == v
            for k, v in {
                "operation_id": str(row.task_id),
                "runner_id": str(row.task.runner_id),
                "attempt": row.attempt,
                "target": row.target,
            }.items()
        )
        safe = (
            exact
            and finished
            and evidence.get("instance_id")
            and evidence.get("quiescent")
        )
        if (
            safe
            and terminal
            and known
            and evidence.get("event") == "workspace:error"
            and (
                row.task.type
                in {"stop_workspace", "resume_workspace", "remove_workspace"}
                and row.payload.get("_retry_count", 0) < 2
            )
        ):
            actions.append("retry")
        if safe and not known and evidence.get("status") in {"terminal", "unknown"}:
            try:
                OrganizationService().require_admin(user, organization_id)
                actions.append("acknowledge_interrupted")
            except AuthenticationError:
                pass
        return actions

    @staticmethod
    def listing(user: Any, organization_id: uuid.UUID) -> list[dict]:
        """List operations visible to an owner or organization administrator."""
        OrganizationService().require_membership(user, organization_id)
        rows = (
            LifecycleCommand.objects.filter(
                task__runner__organization_id=organization_id
            )
            .select_related("task__runner", "task__workspace")
            .order_by("-created_at")[:100]
        )
        visible = []
        for row in rows:
            try:
                DispositionRepository.authorized(user, organization_id, row.task_id)
            except (AuthenticationError, NotFoundError):
                continue
            visible.append(DispositionRepository.summary(row))
        return visible

    @staticmethod
    def dispose(row: LifecycleCommand, evidence: dict, action: str) -> dict:
        """Release only with fresh identity, exclusive-process and runtime proof."""
        if (
            not evidence.get("instance_id")
            or not evidence.get("execution_finished")
            or not evidence.get("quiescent")
            or evidence.get("status") not in {"terminal", "unknown"}
        ):
            raise ConflictError(
                "No fresh proof of finished execution and complete resource scan"
            )
        identity = evidence.get("identity", {})
        if any(
            identity.get(k) != v
            for k, v in {
                "operation_id": str(row.task_id),
                "runner_id": str(row.task.runner_id),
                "attempt": row.attempt,
                "target": row.target,
            }.items()
        ):
            raise ConflictError("Runner evidence identity mismatch")
        if action == "acknowledge_interrupted" and evidence.get("outcome_known"):
            raise ConflictError(
                "Known outcome must be reconciled, not acknowledged as interrupted"
            )
        with transaction.atomic():
            runner = lock_runner(row.task.runner_id)
            if runner.sid != row.task.runner.sid or not runner.is_online:
                raise ConflictError("Runner session changed during inspection")
            ws = None
            if row.task.workspace_id:
                ws = Workspace.objects.select_for_update().get(pk=row.task.workspace_id)
                if ws.current_task_id not in {None, row.task_id}:
                    raise ConflictError("Another operation owns the workspace")
            task = Task.objects.select_for_update().get(pk=row.task_id)
            row = LifecycleCommand.objects.select_for_update().get(pk=task.id)
            if action == "retry" and row.payload.get("_retry_count", 0) >= 2:
                raise ConflictError("Safe retry limit reached")
            if action == "retry" and (
                evidence.get("status") != "terminal"
                or task.type
                not in {"stop_workspace", "resume_workspace", "remove_workspace"}
                or not evidence.get("outcome_known")
                or evidence.get("event") != "workspace:error"
            ):
                raise ConflictError(
                    "Only proven finished failed start/stop/remove may retry"
                )
            if action not in {"acknowledge_interrupted", "retry"}:
                raise ConflictError("Unsupported disposition")
            if task.status == "completed" or row.phase == "disposed":
                raise ConflictError("Operation already resolved")
            task.status = "failed"
            task.error = (
                "Interrupted execution acknowledged after fresh runtime scan; "
                "no resources deleted or recreated"
            )
            from django.utils import timezone

            task.completed_at = timezone.now()
            task.save(update_fields=["status", "error", "completed_at"])
            if action == "acknowledge_interrupted":
                from .models import (
                    CaptureRequest,
                    ImageInstance,
                    WorkspaceRecreateRequest,
                )

                captures = CaptureRequest.objects.filter(child=task)
                capture_image_ids = list(captures.values_list("image_id", flat=True))
                captures.update(
                    phase="failed",
                    resume_suppressed=True,
                    diagnostic=(
                        "Interrupted child acknowledged; resources preserved, "
                        "no automatic restart"
                    ),
                )
                WorkspaceRecreateRequest.objects.filter(child=task).exclude(
                    phase__in=["completed", "failed"]
                ).update(
                    phase="failed",
                    diagnostic=(
                        "Interrupted reset acknowledged; retry the reset or "
                        "delete the workspace"
                    ),
                )
                # The reservation precedes the capture task: an interrupted stop
                # still owns an unfinished image with no creating_task yet.
                ImageInstance.objects.filter(
                    Q(creating_task=task)
                    | Q(pk__in=capture_image_ids, status="capturing")
                ).exclude(
                    status__in=["ready", "deleted", "pending_deletion"]
                ).update(status="failed", updated_at=timezone.now())
            row.phase = "disposed"
            row.save(update_fields=["phase"])
            if ws:
                ws.current_task = None
                ws.active_operation = None
                if task.type in {
                    "create_workspace",
                    "create_workspace_from_image_artifact",
                }:
                    ws.status = "failed"
                elif task.type == "remove_workspace":
                    from .models import WorkspaceRecreateRequest

                    ws.status = (
                        "failed"
                        if WorkspaceRecreateRequest.objects.filter(child=task).exists()
                        else "delete_failed"
                    )
                ws.save(update_fields=["current_task", "active_operation", "status"])
            if action == "retry":
                new = TaskRepository.create(
                    task_id=uuid.uuid4(),
                    runner=runner,
                    task_type=task.type,
                    workspace=ws,
                    operation_payload={
                        **row.payload,
                        "_retry_count": row.payload.get("_retry_count", 0) + 1,
                    },
                )
                return {
                    "operation_id": str(new.id),
                    "previous_operation_id": str(task.id),
                }
            return {
                "operation_id": str(task.id),
                "released": True,
                "resources_preserved": True,
            }
