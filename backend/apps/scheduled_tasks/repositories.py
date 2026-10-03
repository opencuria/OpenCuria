"""Database access for scheduled tasks and occurrence ledger."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import ScheduledTask, ScheduledTaskRun


class ScheduledTaskRepository:
    """ORM boundary for schedule rows and claims."""

    @staticmethod
    def list_for_owner(
        *, organization_id: uuid.UUID, owner_id: int
    ) -> list[ScheduledTask]:
        return list(
            ScheduledTask.objects.filter(
                organization_id=organization_id,
                owner_id=owner_id,
                is_deleted=False,
            ).select_related("workspace")
        )

    @staticmethod
    def get_for_owner(
        task_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
        include_deleted: bool = False,
    ) -> ScheduledTask | None:
        queryset = ScheduledTask.objects.filter(
            id=task_id, organization_id=organization_id, owner_id=owner_id
        )
        if not include_deleted:
            queryset = queryset.filter(is_deleted=False)
        return queryset.select_related("workspace").first()

    @staticmethod
    def create(**fields: object) -> ScheduledTask:
        return ScheduledTask.objects.create(**fields)

    @staticmethod
    def update(task: ScheduledTask, **fields: object) -> ScheduledTask:
        for key, value in fields.items():
            setattr(task, key, value)
        task.save(update_fields=[*fields, "updated_at"])
        return task

    @staticmethod
    def delete(task: ScheduledTask) -> None:
        task.is_deleted = True
        task.enabled = False
        task.save(update_fields=["is_deleted", "enabled", "updated_at"])

    @staticmethod
    def has_active_run(task_id: uuid.UUID) -> bool:
        return ScheduledTaskRun.objects.filter(
            scheduled_task_id=task_id,
            status__in=[
                ScheduledTaskRun.Status.CLAIMED,
                ScheduledTaskRun.Status.RUNNING,
            ],
        ).exists()

    @staticmethod
    def list_runs(task: ScheduledTask, *, limit: int = 100) -> list[ScheduledTaskRun]:
        return list(
            ScheduledTaskRun.objects.filter(
                scheduled_task_id=task.id
            ).select_related("session", "assistant_message").order_by(
                "-scheduled_for"
            )[:limit]
        )

    @staticmethod
    def due(now: datetime) -> list[ScheduledTask]:
        return list(
            ScheduledTask.objects.filter(
                enabled=True, is_deleted=False, next_run_at__lte=now
            ).order_by(
                "next_run_at"
            )[:100]
        )

    @staticmethod
    def has_busy_harness_session(workspace_id: uuid.UUID) -> bool:
        from apps.harness.models import HarnessSession

        return HarnessSession.objects.filter(
            workspace_id=workspace_id, status="busy"
        ).exists()

    @staticmethod
    def _workspace_active_session_query():
        from apps.harness.models import HarnessSession

        return Exists(
            HarnessSession.objects.filter(workspace_id=OuterRef("pk"), status="busy")
        )

    @staticmethod
    def workspace_autostop_settings(
        workspace_id: uuid.UUID,
    ) -> tuple[str, bool, int | None, datetime | None] | None:
        from apps.runners.models import Workspace

        return (
            Workspace.objects.filter(id=workspace_id)
            .annotate(
                has_active_harness_session=(
                    ScheduledTaskRepository._workspace_active_session_query()
                )
            )
            .values_list(
                "status",
                "has_active_harness_session",
                "runner__organization__workspace_auto_stop_timeout_minutes",
                "last_activity_at",
            )
            .first()
        )

    @staticmethod
    def workspace_resume_state(
        workspace_id: uuid.UUID,
    ) -> tuple[str | None, str | None, str | None, bool]:
        from apps.runners.models import Workspace

        row = (
            Workspace.objects.filter(id=workspace_id)
            .values_list(
                "status", "active_operation", "runner__status", "created_by__is_active"
            )
            .first()
        )
        return row or (None, None, None, False)

    @staticmethod
    def workspace_status(workspace_id: uuid.UUID) -> str | None:
        from apps.runners.models import Workspace

        return (
            Workspace.objects.filter(id=workspace_id)
            .values_list("status", flat=True)
            .first()
        )

    @staticmethod
    def touch_workspace_activity(workspace_id: uuid.UUID) -> None:
        from apps.runners.models import Workspace

        Workspace.objects.filter(id=workspace_id).update(
            last_activity_at=timezone.now(), updated_at=timezone.now()
        )

    @staticmethod
    def workspace_is_owned(
        workspace_id: uuid.UUID, *, organization_id: uuid.UUID, owner_id: int
    ) -> bool:
        from apps.organizations.models import Membership
        from apps.runners.models import Workspace

        from django.contrib.auth import get_user_model

        return (
            Workspace.objects.filter(
                id=workspace_id,
                runner__organization_id=organization_id,
                created_by_id=owner_id,
            ).exists()
            and Membership.objects.filter(
                user_id=owner_id, organization_id=organization_id
            ).exists()
            and get_user_model().objects.filter(id=owner_id, is_active=True).exists()
        )

    @staticmethod
    def advance(
        task_id: uuid.UUID, *, expected: datetime, next_run_at: datetime
    ) -> bool:
        return (
            ScheduledTask.objects.filter(
                id=task_id,
                enabled=True,
                is_deleted=False,
                next_run_at=expected,
            ).update(next_run_at=next_run_at, updated_at=timezone.now())
            == 1
        )

    @staticmethod
    def _configuration_snapshot(task: ScheduledTask) -> dict[str, object]:
        """Capture the non-secret occurrence configuration from a locked task."""
        return {
            "name": task.name,
            "workspace_id": str(task.workspace_id),
            "organization_id": str(task.organization_id),
            "prompt": task.prompt,
            "mode": task.mode,
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
            "skill_ids": list(task.skill_ids or []),
            "recurrence": task.recurrence,
            "weekdays": list(task.weekdays or []),
            "local_time": task.local_time.strftime("%H:%M"),
            "timezone_name": task.timezone_name,
        }

    @staticmethod
    def claim(
        task: ScheduledTask,
        *,
        scheduled_for: datetime,
        next_run_at: datetime,
    ) -> ScheduledTaskRun | None:
        try:
            with transaction.atomic():
                locked = (
                    ScheduledTask.objects.select_for_update()
                    .filter(
                        id=task.id,
                        enabled=True,
                        is_deleted=False,
                        next_run_at=scheduled_for,
                    )
                    .first()
                )
                if locked is None:
                    return None
                active = ScheduledTaskRun.objects.filter(
                    scheduled_task=locked,
                    status__in=[
                        ScheduledTaskRun.Status.CLAIMED,
                        ScheduledTaskRun.Status.RUNNING,
                    ],
                ).exists()
                run = ScheduledTaskRun.objects.create(
                    scheduled_task=locked,
                    scheduled_for=scheduled_for,
                    trigger="scheduled",
                    configuration_snapshot=ScheduledTaskRepository._configuration_snapshot(
                        locked
                    ),
                    status=(
                        ScheduledTaskRun.Status.SKIPPED
                        if active
                        else ScheduledTaskRun.Status.CLAIMED
                    ),
                    finished_at=timezone.now() if active else None,
                    reason="task_already_active" if active else "",
                    error=(
                        "This scheduled task already has an active run" if active else ""
                    ),
                )
                locked.next_run_at = next_run_at
                locked.save(update_fields=["next_run_at", "updated_at"])
                return run
        except IntegrityError:
            return None

    @staticmethod
    def create_run(task: ScheduledTask, scheduled_for: datetime) -> ScheduledTaskRun:
        return ScheduledTaskRun.objects.create(
            scheduled_task=task, scheduled_for=scheduled_for
        )

    @staticmethod
    def create_manual_run(
        task: ScheduledTask, scheduled_for: datetime
    ) -> ScheduledTaskRun:
        """Record an immediate run or a skip if this task already has one open."""
        with transaction.atomic():
            locked = ScheduledTask.objects.select_for_update().get(id=task.id)
            active = ScheduledTaskRun.objects.filter(
                scheduled_task=locked,
                status__in=[
                    ScheduledTaskRun.Status.CLAIMED,
                    ScheduledTaskRun.Status.RUNNING,
                ],
            ).exists()
            if active:
                return ScheduledTaskRun.objects.create(
                    scheduled_task=locked,
                    scheduled_for=scheduled_for,
                    trigger="manual",
                    configuration_snapshot=ScheduledTaskRepository._configuration_snapshot(
                        locked
                    ),
                    status=ScheduledTaskRun.Status.SKIPPED,
                    finished_at=timezone.now(),
                    reason="task_already_active",
                    error="This scheduled task already has an active run",
                )
            return ScheduledTaskRun.objects.create(
                scheduled_task=locked,
                scheduled_for=scheduled_for,
                trigger="manual",
                configuration_snapshot=ScheduledTaskRepository._configuration_snapshot(
                    locked
                ),
            )

    @staticmethod
    def runs_for_occurrence(
        task_id: uuid.UUID, scheduled_for: datetime
    ) -> ScheduledTaskRun | None:
        return ScheduledTaskRun.objects.filter(
            scheduled_task_id=task_id, scheduled_for=scheduled_for
        ).first()

    @staticmethod
    def update_run(run: ScheduledTaskRun, **fields: object) -> ScheduledTaskRun:
        for key, value in fields.items():
            setattr(run, key, value)
        run.save(update_fields=[*fields])
        return run

    @staticmethod
    def active_runs(
        *, task_id: uuid.UUID | None = None, message_id: uuid.UUID | None = None
    ) -> list[ScheduledTaskRun]:
        """Find completed assistants whose task ledger still needs settling."""
        rows = ScheduledTaskRun.objects.filter(
            status=ScheduledTaskRun.Status.RUNNING,
            assistant_message__completed_at__isnull=False,
        )
        if task_id is not None:
            rows = rows.filter(scheduled_task_id=task_id)
        if message_id is not None:
            rows = rows.filter(assistant_message_id=message_id)
        return list(rows.select_related("assistant_message", "scheduled_task", "session"))

    @staticmethod
    def finish_run(
        run: ScheduledTaskRun, *, status: str, finished_at: datetime, error: str
    ) -> bool:
        """Settle a linked running ledger once, without overwriting terminal states."""
        fields = {
            "status": status,
            "finished_at": finished_at,
            "error": error,
            "completion_check_pending": True,
        }
        updated = ScheduledTaskRun.objects.filter(
            id=run.id,
            status=ScheduledTaskRun.Status.RUNNING,
            assistant_message_id=run.assistant_message_id,
        ).update(**fields)
        if updated:
            for key, value in fields.items():
                setattr(run, key, value)
        return bool(updated)

    @staticmethod
    def pending_completion_checks() -> list[ScheduledTaskRun]:
        """Keep completion-triggered auto-stop checks durable after eager settlement."""
        return list(
            ScheduledTaskRun.objects.filter(completion_check_pending=True)
            .select_related("scheduled_task", "session")
        )

    @staticmethod
    def clear_completion_check(run_id: uuid.UUID) -> None:
        """Consume a successful completion check, including a policy no-op."""
        ScheduledTaskRun.objects.filter(id=run_id).update(completion_check_pending=False)

    @staticmethod
    def recover_backend_restart() -> int:
        """Recover lost scheduled turns without changing unrelated chat turns."""
        from apps.harness.models import (
            HarnessMessage,
            HarnessPart,
            HarnessSession,
            QuestionRequest,
        )
        from apps.harness.permissions.models import PermissionRequest

        now = timezone.now()
        error = "Backend restarted before the run completed"
        recovered = 0
        with transaction.atomic():
            runs = list(
                ScheduledTaskRun.objects.select_for_update(of=("self",))
                .filter(
                    status__in=[
                        ScheduledTaskRun.Status.CLAIMED,
                        ScheduledTaskRun.Status.RUNNING,
                    ]
                )
                .select_related("assistant_message")
            )
            for run in runs:
                assistant = run.assistant_message
                if assistant is None and run.session_id is not None:
                    assistant = (
                        HarnessMessage.objects.filter(
                            session_id=run.session_id, role="assistant"
                        )
                        .order_by("position", "created_at")
                        .first()
                    )
                if assistant is not None and run.assistant_message_id != assistant.id:
                    run.assistant_message = assistant
                completed = assistant is not None and assistant.completed_at is not None
                if completed:
                    run.status = (
                        ScheduledTaskRun.Status.ERROR
                        if assistant.error or assistant.finish == "error"
                        else ScheduledTaskRun.Status.SUCCEEDED
                    )
                    run.finished_at = assistant.completed_at
                    run.error = assistant.error or ""
                    update_fields = ["status", "finished_at", "error"]
                else:
                    run.status = ScheduledTaskRun.Status.INTERRUPTED
                    run.finished_at = now
                    run.error = error
                    run.reason = "backend_restart"
                    update_fields = ["status", "finished_at", "error", "reason"]
                run.save(update_fields=["assistant_message", *update_fields])
                recovered += 1
                if completed or run.session_id is None:
                    continue

                root_session_id = run.session_id
                if assistant is not None:
                    HarnessMessage.objects.filter(id=assistant.id).update(
                        finish="aborted", error=error, completed_at=now
                    )
                    child_ids = set(
                        HarnessPart.objects.filter(
                            message_id=assistant.id, type="subtask"
                        ).values_list("meta__child_session_id", flat=True)
                    )
                    child_ids.discard(None)
                    child_ids.discard("")
                    session_ids: set[uuid.UUID] = set()
                    frontier = {uuid.UUID(str(child_id)) for child_id in child_ids}
                    while frontier:
                        descendants = set(
                            HarnessSession.objects.filter(
                                id__in=frontier
                            ).values_list("id", flat=True)
                        )
                        descendants -= session_ids
                        if not descendants:
                            break
                        session_ids.update(descendants)
                        frontier = set(
                            HarnessSession.objects.filter(
                                parent_id__in=descendants
                            ).values_list("id", flat=True)
                        ) - session_ids
                    if session_ids:
                        HarnessMessage.objects.filter(
                            session_id__in=session_ids,
                            role="assistant",
                            completed_at__isnull=True,
                        ).update(finish="aborted", error=error, completed_at=now)
                        HarnessSession.objects.filter(id__in=session_ids).update(
                            status="idle"
                        )
                        PermissionRequest.objects.filter(
                            session_id__in=session_ids, status="pending"
                        ).update(status="rejected", resolved_at=now)
                        QuestionRequest.objects.filter(
                            session_id__in=session_ids, status="pending"
                        ).update(status="rejected", resolved_at=now)

                # Do not idle a root session if it has any other incomplete
                # assistant message (for example a follow-up after this run).
                other_incomplete = HarnessMessage.objects.filter(
                    session_id=root_session_id,
                    role="assistant",
                    completed_at__isnull=True,
                )
                if assistant is not None:
                    other_incomplete = other_incomplete.exclude(id=assistant.id)
                if not other_incomplete.exists():
                    HarnessSession.objects.filter(
                        id=root_session_id, status="busy"
                    ).update(status="idle")
                if assistant is not None:
                    PermissionRequest.objects.filter(
                        session_id=root_session_id,
                        status="pending",
                        message_id=assistant.id,
                    ).update(status="rejected", resolved_at=now)
                    QuestionRequest.objects.filter(
                        session_id=root_session_id,
                        status="pending",
                        message_id=assistant.id,
                    ).update(status="rejected", resolved_at=now)
        return recovered

    @staticmethod
    def mark_missed(
        task: ScheduledTask, *, scheduled_for: datetime, next_run_at: datetime
    ) -> None:
        with transaction.atomic():
            try:
                with transaction.atomic():
                    ScheduledTaskRun.objects.create(
                        scheduled_task=task,
                        scheduled_for=scheduled_for,
                        status=ScheduledTaskRun.Status.SKIPPED,
                        finished_at=timezone.now(),
                        reason="missed_occurrence",
                    )
            except IntegrityError:
                pass
            ScheduledTask.objects.filter(
                id=task.id,
                enabled=True,
                is_deleted=False,
                next_run_at=scheduled_for,
            ).update(next_run_at=next_run_at, updated_at=timezone.now())
