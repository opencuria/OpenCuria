# Durable gateway/worker handoff, registrations, and engine-independent run ownership.
import uuid

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("harness", "0029_harnessrun")]

    operations = [
        migrations.AddField(
            model_name="harnessrun",
            name="owner_worker_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="harnessrun",
            name="deployment_version",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="harnessrun",
            name="root_run_id",
            field=models.UUIDField(
                blank=True,
                null=True,
                help_text="Root owner UUID for worker-pinned subagent runs.",
            ),
        ),
        migrations.AddField(
            model_name="harnessrun",
            name="abort_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="HarnessWorkerRegistration",
            fields=[
                (
                    "worker_id",
                    models.CharField(max_length=128, primary_key=True, serialize=False),
                ),
                ("deployment_version", models.CharField(db_index=True, max_length=128)),
                ("pid", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("ready", "Ready"),
                            ("draining", "Draining"),
                            ("stopped", "Stopped"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "heartbeat_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("draining_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "harness_worker_registration",
                "ordering": ["worker_id"],
            },
        ),
        migrations.CreateModel(
            name="HarnessRunCommand",
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
                    "command_type",
                    models.CharField(
                        choices=[
                            ("start", "Start"),
                            ("abort", "Abort"),
                            ("permission", "Permission resolved"),
                            ("question", "Question resolved"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("claimed", "Claimed"),
                            ("done", "Done"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("deployment_version", models.CharField(max_length=128)),
                ("payload", models.JSONField(blank=True, default=dict)),
                (
                    "dedupe_key",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "target_worker_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "claimed_by",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="commands",
                        to="harness.harnessrun",
                    ),
                ),
            ],
            options={
                "db_table": "harness_run_command",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="harnessruncommand",
            index=models.Index(
                fields=["state", "deployment_version", "command_type"],
                name="harness_cmd_claim_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="harnessruncommand",
            constraint=models.UniqueConstraint(
                fields=("run", "command_type", "dedupe_key"),
                name="harness_run_command_dedupe_uniq",
            ),
        ),
    ]
