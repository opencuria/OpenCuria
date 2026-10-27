"""Database access for scheduled tasks and occurrence ledger."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from .models import ScheduledTask, ScheduledTaskRun


class ScheduledTaskRepository:
    """ORM boundary for schedule rows and claims."""

    @staticmethod
    def _with_run_state() -> QuerySet[ScheduledTask]:
        return ScheduledTask.objects.annotate(
            is_running=Exists(
                ScheduledTaskRun.objects.filter(
                    scheduled_task_id=OuterRef("pk"),
                    status__in=[
                        ScheduledTaskRun.Status.CLAIMED,
                        ScheduledTaskRun.Status.RUNNING,
                    ],
                )
            )
        )

    @staticmethod
    def list_for_owner(
        *, organization_id: uuid.UUID, owner_id: int
    ) -> list[ScheduledTask]:
        return list(
            ScheduledTaskRepository._with_run_state().filter(
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
        queryset = ScheduledTaskRepository._with_run_state().filter(
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
    def release_unstarted_claim(
        run_id: uuid.UUID, session_id: uuid.UUID, *, task_id: uuid.UUID
    ) -> bool:
        """Detach an unstarted claim from the chat it alone created.

        Runs under the harness admission locks before the chat is discarded;
        False keeps the chat because the claim started or another run uses it.
        """
        claim = ScheduledTaskRun.objects.select_for_update().filter(
            pk=run_id,
            scheduled_task_id=task_id,
            session_id=session_id,
            status=ScheduledTaskRun.Status.CLAIMED,
            started_at__isnull=True,
            assistant_message__isnull=True,
        ).first()
        if claim is None:
            return False
        if ScheduledTaskRun.objects.filter(session_id=session_id).exclude(
            pk=run_id
        ).exists():
            return False
        claim.session = None
        claim.save(update_fields=["session"])
        return True

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
        """Return recent ledger outcomes without loading assistant answer bodies."""
        return list(
            ScheduledTaskRun.objects.filter(scheduled_task_id=task.id)
            .select_related("assistant_message")
            .defer("assistant_message__content")
            .order_by("-scheduled_for")[:limit]
        )

    @staticmethod
    def run_number(run: ScheduledTaskRun) -> int:
        """Return this task's ledger ordinal, including skipped and manual runs."""
        return ScheduledTaskRun.objects.filter(
            Q(created_at__lt=run.created_at)
            | Q(created_at=run.created_at, pk__lte=run.pk),
            scheduled_task_id=run.scheduled_task_id,
        ).count()

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
            "harness_id": task.harness_id,
            "skill_ids": list(task.skill_ids or []),
            "recurrence": task.recurrence,
            "weekdays": list(task.weekdays or []),
            "cron_expression": task.cron_expression,
            "run_date": task.run_date.isoformat() if task.run_date else None,
            "local_time": task.local_time.strftime("%H:%M"),
            "timezone_name": task.timezone_name,
        }

    @staticmethod
    def _advance_fields(next_run_at: datetime | None) -> dict[str, object]:
        """Move to the next occurrence, or disable a task that has none left.

        A finished one-time task keeps ``next_run_at`` at its only occurrence.
        """
        if next_run_at is None:
            return {"enabled": False}
        return {"next_run_at": next_run_at}

    @staticmethod
    def claim(
        task: ScheduledTask,
        *,
        scheduled_for: datetime,
        next_run_at: datetime | None,
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
                fields = ScheduledTaskRepository._advance_fields(next_run_at)
                for key, value in fields.items():
                    setattr(locked, key, value)
                locked.save(update_fields=[*fields, "updated_at"])
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
        # Completion repair consumes only the assistant outcome, not its answer
        # body or the linked task/session objects.
        return list(
            rows.select_related("assistant_message").defer("assistant_message__content")
        )

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
    @contextmanager
    def restart_runs() -> Iterator[list[ScheduledTaskRun]]:
        """Hold active ledger locks and the transaction used by restart recovery.

        The service settles the ledger and calls the harness recovery service
        inside this scope, so both contexts roll back together on failure.
        """
        with transaction.atomic():
            yield list(
                ScheduledTaskRun.objects.select_for_update(of=("self",))
                .filter(
                    status__in=[
                        ScheduledTaskRun.Status.CLAIMED,
                        ScheduledTaskRun.Status.RUNNING,
                    ]
                )
                .select_related("assistant_message")
            )

    @staticmethod
    def mark_missed(
        task: ScheduledTask, *, scheduled_for: datetime, next_run_at: datetime | None
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
            ).update(
                **ScheduledTaskRepository._advance_fields(next_run_at),
                updated_at=timezone.now(),
            )
