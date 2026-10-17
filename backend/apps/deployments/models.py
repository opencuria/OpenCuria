"""Database records used to enforce backend deployment compatibility."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class Release(models.Model):
    """An immutable backend/worker release retained by this deployment."""

    version = models.CharField(max_length=255, primary_key=True)
    protocol_min = models.PositiveIntegerField()
    protocol_max = models.PositiveIntegerField()
    database_schema = models.PositiveIntegerField()
    database_schema_min = models.PositiveIntegerField()
    database_schema_max = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=(
            ("active", "Active"),
            ("draining", "Draining"),
            ("rollback", "Rollback retained"),
            ("retired", "Retired"),
        ),
        default="active",
    )
    registered_at = models.DateTimeField(default=timezone.now)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Sort retained release metadata predictably."""

        ordering = ("registered_at", "version")

    def __str__(self) -> str:
        """Return this release's immutable identifier."""
        return self.version


class DatabaseSchemaState(models.Model):
    """Monotonic schema marker; rollback never moves this value backwards."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    schema_version = models.PositiveIntegerField()
    updated_at = models.DateTimeField(default=timezone.now)


class LifecycleRecoveryLease(models.Model):
    """Generation identity of the unique recovery process holding the PG lock."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    owner_id = models.CharField(max_length=128, blank=True, default="")
    deployment_version = models.CharField(max_length=255, blank=True, default="")
    owner_token = models.UUIDField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "deployments_lifecyclerecoverylease"
