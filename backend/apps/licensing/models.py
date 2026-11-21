"""Windows guest licensing records.

- ``WindowsLicensePolicy``: one row per organization (created lazily) with the
  license model, the KMS host, the user license seats and the running cap.
- ``WindowsProductKey``: a Retail/MAK key with a seat count; the key is
  encrypted at rest and only its last five characters are shown.
- ``WindowsUserLicense``: an organization member licensed for Windows
  (per-user model, activation through KMS).
- ``WindowsWorkspaceLicense``: the license a Windows workspace holds. A
  ``device_key`` row occupies one seat of its key for the life of the
  workspace; the row is deleted when the workspace deletion is confirmed.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class WindowsLicenseMode(models.TextChoices):
    """How an organization licenses its Windows workspaces."""

    PER_DEVICE = "per_device", "Product keys"
    PER_USER = "per_user", "Per user"
    UNLICENSED = "unlicensed", "Without license"


class WindowsLicenseKind(models.TextChoices):
    """What a Windows workspace is licensed by."""

    DEVICE_KEY = "device_key", "Product key seat"
    USER = "user", "User license"
    UNLICENSED = "unlicensed", "Unlicensed"


class WindowsActivationStatus(models.TextChoices):
    """Last activation result reported by the runner."""

    PENDING = "pending", "Pending"
    ACTIVATED = "activated", "Activated"
    FAILED = "failed", "Failed"
    NOT_ACTIVATED = "not_activated", "Not activated"


class WindowsLicensePolicy(models.Model):
    """Organization-wide Windows license settings (admin only)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="windows_license_policy",
    )
    mode = models.CharField(
        max_length=20,
        choices=WindowsLicenseMode.choices,
        default=WindowsLicenseMode.PER_DEVICE,
    )
    kms_host = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="KMS host or host:port; empty uses DNS auto-discovery.",
    )
    user_license_seats = models.PositiveIntegerField(default=0)
    max_running = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Running Windows workspaces allowed at once; empty = unlimited.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "licensing_windows_policy"

    def __str__(self) -> str:
        return f"WindowsLicensePolicy(org={self.organization_id}, {self.mode})"


class WindowsProductKey(models.Model):
    """A Windows product key with a number of device seats."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="windows_product_keys",
    )
    label = models.CharField(max_length=255, blank=True, default="")
    encrypted_key = models.TextField()
    key_suffix = models.CharField(max_length=5)
    fingerprint = models.CharField(max_length=64)
    seats = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "licensing_windows_product_key"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fingerprint"],
                name="licensing_windows_key_unique_per_org",
            ),
            models.CheckConstraint(
                condition=models.Q(seats__gte=1),
                name="licensing_windows_key_seats_gte_1",
            ),
        ]

    def __str__(self) -> str:
        return f"WindowsProductKey(…{self.key_suffix}, seats={self.seats})"


class WindowsUserLicense(models.Model):
    """An organization member licensed for Windows (per-user model)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="windows_user_licenses",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="windows_user_licenses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "licensing_windows_user_license"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="licensing_windows_user_license_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"WindowsUserLicense(org={self.organization_id}, user={self.user_id})"


class WindowsWorkspaceLicense(models.Model):
    """The license a Windows workspace holds; seats used = rows per key."""

    workspace = models.OneToOneField(
        "runners.Workspace",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="windows_license",
    )
    kind = models.CharField(max_length=20, choices=WindowsLicenseKind.choices)
    # RESTRICT: a key with assigned seats cannot be deleted on its own, but an
    # organization teardown that also deletes the workspaces may remove both.
    product_key = models.ForeignKey(
        WindowsProductKey,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="assignments",
    )
    activation_status = models.CharField(
        max_length=20,
        choices=WindowsActivationStatus.choices,
        default=WindowsActivationStatus.PENDING,
    )
    activation_code = models.CharField(max_length=32, blank=True, default="")
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "licensing_windows_workspace_license"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind=WindowsLicenseKind.DEVICE_KEY,
                        product_key__isnull=False,
                    )
                    | (
                        ~models.Q(kind=WindowsLicenseKind.DEVICE_KEY)
                        & models.Q(product_key__isnull=True)
                    )
                ),
                name="licensing_windows_seat_has_key",
            ),
        ]

    def __str__(self) -> str:
        return f"WindowsWorkspaceLicense(ws={self.workspace_id}, {self.kind})"
