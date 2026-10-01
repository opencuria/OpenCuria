from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class ScheduledTask(models.Model):
    """A user's recurring prompt bound to an owned workspace."""

    class Recurrence(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    workspace = models.ForeignKey("runners.Workspace", on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    prompt = models.TextField()
    mode = models.CharField(
        max_length=16,
        choices=(("plan", "Plan"), ("build", "Build")),
        default="build",
    )
    model = models.CharField(max_length=255, blank=True, default="")
    reasoning_effort = models.CharField(max_length=50, blank=True, default="")
    skill_ids = models.JSONField(default=list, blank=True)
    recurrence = models.CharField(max_length=16, choices=Recurrence.choices)
    weekdays = models.JSONField(default=list, blank=True)
    local_time = models.TimeField()
    timezone_name = models.CharField(max_length=64, default="UTC")
    enabled = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    next_run_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduled_tasks_task"
        ordering = ["next_run_at", "created_at"]
        indexes = [
            models.Index(
                fields=["enabled", "next_run_at"], name="sched_enabled_next_idx"
            )
        ]


class ScheduledTaskSchedulerLease(models.Model):
    """Singleton database lease guarding the scheduler process."""

    key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    owner_token = models.UUIDField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "scheduled_tasks_scheduler_lease"


class ScheduledTaskRun(models.Model):
    """One durable dispatch ledger row for a scheduled occurrence."""

    class Status(models.TextChoices):
        CLAIMED = "claimed", "Claimed"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        ERROR = "error", "Error"
        SKIPPED = "skipped", "Skipped"
        INTERRUPTED = "interrupted", "Interrupted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheduled_task = models.ForeignKey(
        ScheduledTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="runs",
    )
    scheduled_for = models.DateTimeField()
    trigger = models.CharField(max_length=16, default="scheduled")
    configuration_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CLAIMED
    )
    session = models.ForeignKey(
        "harness.HarnessSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scheduled_task_runs",
    )
    assistant_message = models.ForeignKey(
        "harness.HarnessMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scheduled_task_runs",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    reason = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scheduled_tasks_run"
        ordering = ["-scheduled_for"]
        constraints = [
            models.UniqueConstraint(
                fields=["scheduled_task", "scheduled_for"],
                name="sched_task_occurrence_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "scheduled_for"], name="sched_run_status_next_idx"
            )
        ]
