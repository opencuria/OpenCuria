"""Expo apps that workspace agents publish to their user's mobile app.

An app is identified by its Expo ``slug`` per owner and organization; every
publish adds an immutable version. Version files are content-addressed blobs,
so unchanged bundles and assets are stored once across versions and apps.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


def blob_upload_to(instance: AppBlob, _filename: str) -> str:
    """Store blobs by content hash, fanned out over 256 directories."""
    return f"apps/blobs/{instance.sha256[:2]}/{instance.sha256}"


class AppBlob(models.Model):
    """One immutable file (JS bundle, asset or icon) keyed by its SHA-256."""

    sha256 = models.CharField(primary_key=True, max_length=64)
    md5 = models.CharField(max_length=32)
    size = models.PositiveBigIntegerField()
    file = models.FileField(upload_to=blob_upload_to, max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mobile_apps_blob"


class App(models.Model):
    """An Expo app published to one user, identified by its Expo slug."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # The workspace that last published the app; kept when it is deleted.
    workspace = models.ForeignKey(
        "runners.Workspace", null=True, blank=True, on_delete=models.SET_NULL
    )
    slug = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    # Expo project directory in the workspace, so later chats find the source.
    project_path = models.CharField(max_length=1024, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mobile_apps_app"
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "owner", "slug"],
                name="mobile_apps_unique_owner_slug",
            )
        ]


class AppVersion(models.Model):
    """One immutable published build of an app."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="versions")
    # Sequential per app, starting at 1; the newest version is the one offered.
    number = models.PositiveIntegerField()
    # The Expo config ``version`` (e.g. "1.2.0"), shown to the user.
    version_name = models.CharField(max_length=64)
    sdk_version = models.CharField(max_length=32)
    notes = models.TextField(blank=True, default="")
    # Public Expo config (``expo config --type public``) the app reads as
    # ``Constants.expoConfig``.
    expo_config = models.JSONField(default=dict)
    icon = models.ForeignKey(
        AppBlob,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mobile_apps_version"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["app", "number"], name="mobile_apps_unique_version_number"
            )
        ]


class AppFile(models.Model):
    """A file of a version on one platform: its launch bundle or an asset."""

    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"

    class Kind(models.TextChoices):
        BUNDLE = "bundle", "Bundle"
        ASSET = "asset", "Asset"

    version = models.ForeignKey(
        AppVersion, on_delete=models.CASCADE, related_name="files"
    )
    platform = models.CharField(max_length=16, choices=Platform.choices)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Metro asset hash the bundle references (empty for the bundle itself).
    key = models.CharField(max_length=64, blank=True, default="")
    extension = models.CharField(max_length=16, blank=True, default="")
    blob = models.ForeignKey(AppBlob, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "mobile_apps_file"
        constraints = [
            models.UniqueConstraint(
                fields=["version", "platform", "kind", "key"],
                name="mobile_apps_unique_version_file",
            )
        ]
        indexes = [
            models.Index(fields=["version", "platform"], name="mobile_apps_file_vp_idx")
        ]
