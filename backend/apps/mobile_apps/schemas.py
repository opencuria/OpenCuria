"""Pydantic schemas for the apps REST API."""

from __future__ import annotations

import uuid
from datetime import datetime

from ninja import Schema


class AppVersionOut(Schema):
    """One published version."""

    id: uuid.UUID
    number: int
    version_name: str
    sdk_version: str
    notes: str
    created_at: datetime
    #: Content hash of the icon PNG; download it as a version file.
    icon_sha256: str | None = None
    #: Download size in bytes per platform the version supports.
    platforms: dict[str, int]


class AppOut(Schema):
    """An app with its newest version."""

    id: uuid.UUID
    name: str
    slug: str
    workspace_id: uuid.UUID | None = None
    workspace_name: str = ""
    created_at: datetime
    updated_at: datetime
    latest_version: AppVersionOut | None = None


class AppDetailOut(AppOut):
    """An app with all retained versions, newest first."""

    versions: list[AppVersionOut]


class AppFileOut(Schema):
    """A downloadable, content-addressed file."""

    sha256: str
    md5: str
    size: int


class AppAssetOut(AppFileOut):
    """An asset the bundle references by its Metro hash."""

    key: str
    extension: str


class AppManifestOut(Schema):
    """What a device downloads to install one version on one platform."""

    app_id: uuid.UUID
    version_id: uuid.UUID
    number: int
    name: str
    slug: str
    version_name: str
    sdk_version: str
    platform: str
    expo_config: dict
    bundle: AppFileOut
    assets: list[AppAssetOut]
    icon: AppFileOut | None = None
    created_at: datetime
