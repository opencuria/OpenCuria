"""Persistent requests crossing the API/execution process boundary."""

from __future__ import annotations

import uuid

from django.db import models

ADMISSION_COMMAND_KINDS = frozenset({"start", "edit"})
CONTROL_COMMAND_KINDS = frozenset(
    {
        "abort",
        "delete",
        "steer",
        "withdraw",
        "permission",
        "question",
        "abort_computeruse",
    }
)


class HarnessCommand(models.Model):
    """One durable request; claimed effects are never automatically executed twice."""

    class State(models.TextChoices):
        QUEUED = "queued"
        CLAIMED = "claimed"
        COMPLETED = "completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=64)
    session_id = models.UUIDField(null=True, blank=True, db_index=True)
    payload_encrypted = models.TextField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    owner_id = models.UUIDField(null=True, blank=True)
    result_encrypted = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "harness_command"
        indexes = [
            models.Index(fields=["state", "created_at"], name="harness_command_queue"),
            models.Index(
                fields=["state", "kind", "created_at"], name="harness_command_lane"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="queued",
                        owner_id__isnull=True,
                        claimed_at__isnull=True,
                        completed_at__isnull=True,
                    )
                    | models.Q(
                        state="claimed",
                        owner_id__isnull=False,
                        claimed_at__isnull=False,
                        completed_at__isnull=True,
                    )
                    | models.Q(
                        state="completed",
                        owner_id__isnull=False,
                        claimed_at__isnull=False,
                        completed_at__isnull=False,
                    )
                ),
                name="harness_command_valid_state",
            )
        ]


class HarnessSteeringSnapshot(models.Model):
    """Encrypted projection of the execution owner's current steering queue."""

    session = models.OneToOneField(
        "harness.HarnessSession",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="steering_snapshot",
    )
    payload_encrypted = models.TextField()
