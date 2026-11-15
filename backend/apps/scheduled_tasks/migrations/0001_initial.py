# Generated for recurring scheduled tasks.
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("harness", "0026_backfill_harnesspart_display"),
        ("organizations", "0001_initial"),
        ("runners", "0016_workspaceprocess_kind_temp"),
    ]
    operations = [
        migrations.CreateModel(
            name="ScheduledTask",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        serialize=False,
                        editable=False,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("prompt", models.TextField()),
                (
                    "mode",
                    models.CharField(
                        max_length=16,
                        choices=[("plan", "Plan"), ("build", "Build")],
                        default="build",
                    ),
                ),
                ("model", models.CharField(max_length=255, blank=True, default="")),
                (
                    "reasoning_effort",
                    models.CharField(max_length=50, blank=True, default=""),
                ),
                ("skill_ids", models.JSONField(default=list, blank=True)),
                (
                    "recurrence",
                    models.CharField(
                        max_length=16,
                        choices=[("daily", "Daily"), ("weekly", "Weekly")],
                    ),
                ),
                ("weekdays", models.JSONField(default=list, blank=True)),
                ("local_time", models.TimeField()),
                ("timezone_name", models.CharField(max_length=64, default="UTC")),
                ("enabled", models.BooleanField(default=True)),
                ("next_run_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        to="organizations.organization",
                        on_delete=django.db.models.deletion.CASCADE,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        to=settings.AUTH_USER_MODEL,
                        on_delete=django.db.models.deletion.CASCADE,
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        to="runners.workspace",
                        on_delete=django.db.models.deletion.CASCADE,
                    ),
                ),
            ],
            options={
                "db_table": "scheduled_tasks_task",
                "ordering": ["next_run_at", "created_at"],
                "indexes": [
                    models.Index(
                        fields=["enabled", "next_run_at"], name="sched_enabled_next_idx"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ScheduledTaskRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        serialize=False,
                        editable=False,
                    ),
                ),
                ("scheduled_for", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        max_length=16,
                        choices=[
                            ("claimed", "Claimed"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("error", "Error"),
                            ("skipped", "Skipped"),
                            ("interrupted", "Interrupted"),
                        ],
                        default="claimed",
                    ),
                ),
                ("started_at", models.DateTimeField(null=True, blank=True)),
                ("finished_at", models.DateTimeField(null=True, blank=True)),
                ("error", models.TextField(blank=True, default="")),
                ("reason", models.CharField(max_length=64, blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "assistant_message",
                    models.ForeignKey(
                        to="harness.harnessmessage",
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="scheduled_task_runs",
                    ),
                ),
                (
                    "scheduled_task",
                    models.ForeignKey(
                        to="scheduled_tasks.scheduledtask",
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="runs",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        to="harness.harnesssession",
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="scheduled_task_runs",
                    ),
                ),
            ],
            options={
                "db_table": "scheduled_tasks_run",
                "ordering": ["-scheduled_for"],
                "indexes": [
                    models.Index(
                        fields=["status", "scheduled_for"],
                        name="sched_run_status_next_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scheduled_task", "scheduled_for"),
                        name="sched_task_occurrence_uniq",
                    )
                ],
            },
        ),
    ]
