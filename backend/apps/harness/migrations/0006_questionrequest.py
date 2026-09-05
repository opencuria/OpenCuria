"""Add QuestionRequest model for the harness question tool."""

from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    """Create harness_question_request table."""

    dependencies = [
        ("harness", "0005_harnesssession_last_read_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="QuestionRequest",
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
                        help_text="Owning organization (scoping, no FK to avoid cycles).",
                    ),
                ),
                ("workspace_id", models.UUIDField(blank=True, null=True)),
                (
                    "session_id",
                    models.UUIDField(help_text="Harness session UUID."),
                ),
                ("message_id", models.UUIDField(blank=True, null=True)),
                (
                    "call_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "questions",
                    models.JSONField(
                        default=list,
                        help_text="Structured question schema shown to the user.",
                    ),
                ),
                (
                    "answers",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="User-provided answers (list aligned with questions).",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("answered", "Answered"),
                            ("rejected", "Rejected"),
                            ("timed_out", "Timed out"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "harness_question_request",
                "ordering": ["-created_at"],
            },
        ),
    ]
