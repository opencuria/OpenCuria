"""REST schemas for Windows licensing administration.

Responses never contain product keys; only their last five characters.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import SecretStr


class WindowsKeyAssignmentOut(Schema):
    """One workspace seat of a key, shown by owner label only."""

    owner_label: str
    activation_status: str


class WindowsProductKeyOut(Schema):
    """A product key of the pool (write-only key, display suffix only)."""

    id: uuid.UUID
    label: str
    key_suffix: str
    seats: int
    used_seats: int
    created_at: datetime
    assignments: list[WindowsKeyAssignmentOut]


class WindowsLicenseMemberOut(Schema):
    """An organization member and whether they hold a Windows user license.

    ``user_id`` is the user's primary key as a string (clients treat it as an
    opaque identifier for the ``users/{user_id}/`` endpoints).
    """

    user_id: str
    label: str
    licensed: bool


class WindowsLicenseUsageOut(Schema):
    """Live Windows workspaces of the organization by license kind."""

    windows_workspaces: int
    running: int
    device_key: int
    user: int
    unlicensed: int


class WindowsLicensingOut(Schema):
    """The organization's Windows license settings and usage (admins only)."""

    mode: Literal["per_device", "per_user", "unlicensed"]
    kms_host: str
    user_license_seats: int
    max_running: int | None
    product_keys: list[WindowsProductKeyOut]
    members: list[WindowsLicenseMemberOut]
    usage: WindowsLicenseUsageOut


class WindowsLicensingUpdateIn(Schema):
    """Partial policy update; an explicit ``max_running: null`` clears the cap."""

    mode: str | None = None
    kms_host: str | None = None
    user_license_seats: int | None = None
    max_running: int | None = None


class WindowsProductKeyCreateIn(Schema):
    """Add a Retail or MAK product key with a number of device seats."""

    product_key: SecretStr
    label: str = ""
    seats: int = 1


class WindowsProductKeyUpdateIn(Schema):
    """Change a key's label or seats; the key itself cannot be changed."""

    label: str | None = None
    seats: int | None = None
