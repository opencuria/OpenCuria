"""Public native-device contracts; no provider token is ever echoed."""

from __future__ import annotations

import uuid
from typing import Literal

from ninja import Schema
from pydantic import Field, field_validator

APP_IDS = {"com.opencuria.mobile", "com.opencuria.mobile.dev"}


class DeviceRegistrationIn(Schema):
    platform: Literal["ios", "android"]
    app_id: str
    apns_environment: Literal["sandbox", "production"] = "production"
    push_token: str = Field(default="", max_length=4096)
    notifications_enabled: bool = False
    live_activities_enabled: bool = False

    @field_validator("app_id")
    @classmethod
    def validate_app_id(cls, value: str) -> str:
        """Do not allow clients to send pushes to arbitrary applications."""
        if value not in APP_IDS:
            raise ValueError("Unsupported application identifier")
        return value

    @field_validator("push_token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        """Provider tokens are opaque, printable, whitespace-free values."""
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError("Invalid push token")
        return value


class ActivityRegistrationIn(Schema):
    activity_id: str = Field(min_length=1, max_length=255)
    push_token: str = Field(min_length=32, max_length=512, pattern=r"^[a-fA-F0-9]+$")


class PushConfigurationIn(Schema):
    apns_credential_id: uuid.UUID | None = None
    fcm_credential_id: uuid.UUID | None = None


class PushCapabilitiesOut(Schema):
    ios: bool
    android: bool
    live_activities: bool


class PushConfigurationOut(PushCapabilitiesOut):
    apns_credential_id: uuid.UUID | None
    fcm_credential_id: uuid.UUID | None


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
