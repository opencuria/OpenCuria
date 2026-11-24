"""Public native-device contracts; handles and secrets are never echoed."""

from __future__ import annotations

import uuid
from typing import Literal

from ninja import Schema
from pydantic import Field, field_validator

from .encryption import decode_b64url, validate_receiver_key

_HANDLE = r"^[A-Za-z0-9_-]+$"


class DeviceRegistrationIn(Schema):
    """Opt-ins plus this installation's push proxy handle and encryption key."""

    platform: Literal["ios", "android"]
    push_handle: str = Field(default="", max_length=8192, pattern=r"^[A-Za-z0-9_-]*$")
    encryption_key: str = Field(default="", max_length=100)
    auth_secret: str = Field(default="", max_length=32)
    notifications_enabled: bool = False
    live_activities_enabled: bool = False

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        """Only valid P-256 receiver keys can be encrypted to."""
        if value:
            validate_receiver_key(value)
        return value

    @field_validator("auth_secret")
    @classmethod
    def validate_auth_secret(cls, value: str) -> str:
        """RFC 8291 auth secrets are 16 random bytes."""
        if value and len(decode_b64url(value)) != 16:
            raise ValueError("The auth secret must be 16 bytes")
        return value


class ActivityRegistrationIn(Schema):
    activity_id: str = Field(min_length=1, max_length=255)
    push_handle: str = Field(min_length=1, max_length=8192, pattern=_HANDLE)


class PushCapabilitiesOut(Schema):
    """The push proxy this server uses and the key it signs with (null: off)."""

    proxy_url: str | None
    server_key: str | None


class DeviceOut(Schema):
    id: uuid.UUID
    notifications_enabled: bool
    live_activities_enabled: bool


class ActivityRowOut(Schema):
    sessionId: str  # noqa: N815 - shared native widget wire format
    workspaceId: str  # noqa: N815
    title: str
    workspaceName: str  # noqa: N815
    phase: str


class ActivitySnapshotOut(Schema):
    organizationId: str  # noqa: N815
    deviceId: str  # noqa: N815
    activeCount: int  # noqa: N815
    activities: list[ActivityRowOut]
    updatedAt: str  # noqa: N815
