# Durable ownership for external-engine assistant-turn attempts.
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("harness", "0028_claude_harness"),
    ]

    operations = [
        migrations.CreateModel(
            name="HarnessRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "organization_id",
                    models.UUIDField(
                        help_text="Organization snapshot for durable attempt scoping."
                    ),
                ),
                (
                    "harness_id",
                    models.CharField(default="claude", max_length=16),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("starting", "Starting"),
                            ("running", "Running"),
                            ("closing", "Closing"),
                            ("completed", "Completed"),
                            ("interrupted", "Interrupted"),
                            ("error", "Error"),
                        ],
                        db_index=True,
                        default="starting",
                        max_length=16,
                    ),
                ),
                (
                    "owner_token",
                    models.UUIDField(default=uuid.uuid4, editable=False),
                ),
                (
                    "heartbeat_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("lease_id", models.UUIDField(blank=True, null=True)),
                (
                    "lease_epoch",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "external_session_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "assistant_message",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="run_attempt",
                        to="harness.harnessmessage",
                    ),
                ),
                (
                    "initiated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="harness_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runs",
                        to="harness.harnesssession",
                    ),
                ),
            ],
            options={
                "db_table": "harness_run",
                "ordering": ["created_at"],
                "indexes": [
                    models.Index(
                        fields=["session", "status"],
                        name="harness_run_session_status_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("session",),
                        condition=models.Q(
                            status__in=["starting", "running", "closing"]
                        ),
                        name="harness_run_one_live_per_session",
                    ),
                ],
            },
        ),
    ]
