"""Per-answer provider-account snapshots and revocation state."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ProviderTurnMutex(models.Model):
    """A database-backed admission/revocation serialization point."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        db_table = "harness_provider_turn_mutex"


class ProviderTurn(models.Model):
    """Account identity snapshot and cancellation state for one answer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assistant_message = models.OneToOneField(
        "harness.HarnessMessage",
        on_delete=models.CASCADE,
        related_name="provider_turn",
    )
    organization_id = models.UUIDField(db_index=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="harness_provider_turns",
    )
    # Mapping provider -> ProviderConnection UUID. Missing providers are frozen
    # as absent; a later selection is never consulted during this answer.
    account_ids = models.JSONField(default=dict, blank=True)
    # Providers actually resolved by this answer (including title/grounding).
    used_account_ids = models.JSONField(default=list, blank=True)
    # Also records dependencies on related work such as title/summary tasks.
    dependency_account_ids = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    cancel_requested = models.BooleanField(default=False, db_index=True)
    revoked_account_id = models.UUIDField(null=True, blank=True)
    holds = models.PositiveSmallIntegerField(default=1)
    # Holder tokens make normal completion, startup rollback and cancellation
    # cleanup idempotent rather than relying on blind decrement counts.
    holder_tokens = models.JSONField(default=list, blank=True)
    monitor_required = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "harness_provider_turn"
        indexes = [
            models.Index(
                fields=["active", "cancel_requested"],
                name="harness_provider_turn_live_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return a secret-free identifier for an answer snapshot."""
        return f"ProviderTurn({str(self.id)[:8]}, active={self.active})"
