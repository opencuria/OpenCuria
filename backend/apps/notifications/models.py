"""Push proxy destinations and a durable, bounded delivery outbox."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class MobileDevice(models.Model):
    """One installation, scoped to the signed-in user and organization.

    IDs are random installation capabilities, not hardware identifiers. The
    backend never sees native APNs/FCM tokens: it stores the push proxy's opaque
    handles and the device's public encryption key. Handles and the auth secret
    are encrypted at rest and never returned through the public API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    platform = models.CharField(
        max_length=7, choices=[("ios", "iOS"), ("android", "Android")]
    )
    encrypted_push_handle = models.TextField(blank=True, default="")
    push_handle_hash = models.CharField(
        max_length=64, blank=True, default="", db_index=True
    )
    # RFC 8291 receiver key (public) and auth secret of this installation.
    encryption_key = models.CharField(max_length=100, blank=True, default="")
    encrypted_auth_secret = models.TextField(blank=True, default="")
    notifications_enabled = models.BooleanField(default=False)
    notifications_enabled_at = models.DateTimeField(null=True, blank=True)
    live_activities_enabled = models.BooleanField(default=False)
    activity_id = models.CharField(max_length=255, blank=True, default="")
    encrypted_activity_handle = models.TextField(blank=True, default="")
    activity_updated_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)


class PushDelivery(models.Model):
    """Idempotent work that survives process restarts and proxy outages."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(MobileDevice, on_delete=models.CASCADE)
    event_key = models.CharField(max_length=255)
    kind = models.CharField(
        max_length=8, choices=[("alert", "Alert"), ("activity", "Activity")]
    )
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, default="pending", db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    # Only bounded proxy error codes; never response bodies or destinations.
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "event_key", "kind"],
                name="mobile_delivery_event_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "available_at"], name="mobile_delivery_due_idx"
            )
        ]
