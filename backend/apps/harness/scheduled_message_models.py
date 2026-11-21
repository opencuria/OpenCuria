"""User messages waiting to be sent into their conversation at a chosen time."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class HarnessScheduledMessage(models.Model):
    """One message a user scheduled for a conversation.

    The row only exists until delivery: a sent message becomes a regular user
    turn (or a steer of the running turn) and its row is deleted. ``sending``
    fences concurrent deliveries; ``failed`` keeps the text and reason so the
    user can retry, reschedule or edit it.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "harness.HarnessSession",
        on_delete=models.CASCADE,
        related_name="scheduled_messages",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="harness_scheduled_messages",
    )
    prompt = models.TextField()
    # Run options applied when the message starts a new turn; empty keeps the
    # session's current choice. A steer of a running turn ignores them.
    mode = models.CharField(max_length=16, blank=True, default="")
    model = models.CharField(max_length=255, blank=True, default="")
    reasoning_effort = models.CharField(max_length=50, blank=True, default="")
    skill_ids = models.JSONField(default=list, blank=True)
    send_at = models.DateTimeField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    error = models.TextField(blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_scheduled_message"
        ordering = ["send_at", "created_at"]
        indexes = [
            models.Index(
                fields=["status", "send_at"], name="harness_sched_msg_due_idx"
            ),
        ]

    def __str__(self) -> str:
        """Return a prompt-free description."""
        return (
            f"HarnessScheduledMessage(session={self.session_id}, "
            f"status={self.status}, send_at={self.send_at.isoformat()})"
        )
