"""Durable, encrypted state for isolated ChatGPT device OAuth flows."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class ProviderOAuthFlow(models.Model):
    """One user's pending device flow and its exact target provider account."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        POLLING = "polling", "Polling"
        CONSUMED = "consumed", "Consumed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        DENIED = "denied", "Denied"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="provider_oauth_flows",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="provider_oauth_flows",
    )
    target_account_id = models.UUIDField(null=True, blank=True)
    label = models.CharField(max_length=255, default="ChatGPT", blank=True)
    shared = models.BooleanField(default=False)
    device_auth_id_encrypted = models.TextField()
    user_code_encrypted = models.TextField()
    interval = models.PositiveSmallIntegerField(default=5)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    poll_token = models.UUIDField(null=True, blank=True)
    connected_account_id = models.CharField(max_length=255, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_provider_oauth_flow"
        indexes = [
            models.Index(
                fields=["organization", "user", "status"],
                name="harness_oauth_flow_owner_idx",
            )
        ]

    def __str__(self) -> str:
        """Return a non-secret representation."""
        return f"ProviderOAuthFlow({self.id}, status={self.status})"
