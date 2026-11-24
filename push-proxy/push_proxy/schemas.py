"""Wire contracts of the proxy API (protocol version 1)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .encoding import b64url_decode, decode_server_key

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_PRINTABLE = re.compile(r"^[\x21-\x7e]+$")
# aes128gcm (RFC 8188) header: salt(16) + rs(4) + idlen(1) + sender key(65).
_HEADER = 86
_MIN_CIPHERTEXT = _HEADER + 17


class RegistrationIn(BaseModel):
    """A device or Live Activity token, sealed for one backend key."""

    model_config = ConfigDict(extra="forbid")

    platform: Literal["ios", "android"]
    app_id: str = Field(min_length=1, max_length=255)
    environment: Literal["production", "sandbox"] = "production"
    kind: Literal["alert", "activity"] = "alert"
    token: str = Field(min_length=1, max_length=4096)
    server_key: str = Field(max_length=128)

    @field_validator("server_key")
    @classmethod
    def validate_server_key(cls, value: str) -> str:
        """Bind handles only to valid P-256 backend keys."""
        decode_server_key(value)
        return value

    @model_validator(mode="after")
    def validate_destination(self) -> RegistrationIn:
        """Live Activities exist on iOS only; tokens use each platform's format."""
        if self.platform == "android":
            if self.kind != "alert" or self.environment != "production":
                raise ValueError("Android supports production notifications only")
            if not _PRINTABLE.match(self.token):
                raise ValueError("Invalid FCM token")
        elif not (32 <= len(self.token) <= 512 and _HEX.match(self.token)):
            raise ValueError("Invalid APNs token")
        return self


class RegistrationOut(BaseModel):
    handle: str


class PushIn(BaseModel):
    """One encrypted message for one handle."""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=8192)
    type: Literal["alert", "activity_update", "activity_end"]
    payload: str = Field(max_length=4096)
    ttl: int = Field(default=900, ge=0, le=86_400)
    collapse_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    timestamp: int | None = Field(default=None, ge=0)
    stale_date: int | None = Field(default=None, ge=0)
    dismissal_date: int | None = Field(default=None, ge=0)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: str) -> str:
        """Only forward bytes shaped like one aes128gcm record from a P-256 sender."""
        raw = b64url_decode(value)
        if len(raw) < _MIN_CIPHERTEXT or raw[20] != 65 or raw[21] != 0x04:
            raise ValueError("payload must be aes128gcm ciphertext (RFC 8291)")
        record_size = int.from_bytes(raw[16:20], "big")
        if record_size < 18 or len(raw) - _HEADER > record_size:
            raise ValueError("payload must be a single aes128gcm record")
        return value

    @model_validator(mode="after")
    def validate_activity(self) -> PushIn:
        """Live Activity updates need a timestamp so iOS can order them."""
        dates = (self.timestamp, self.stale_date, self.dismissal_date)
        if self.type == "alert":
            if any(date is not None for date in dates):
                raise ValueError("Alerts do not take Live Activity dates")
        elif self.collapse_id is not None:
            raise ValueError("Live Activity updates always replace each other")
        elif self.timestamp is None:
            raise ValueError("Live Activity updates need a timestamp")
        elif self.type == "activity_update" and self.dismissal_date is not None:
            raise ValueError("Only activity_end takes a dismissal_date")
        return self
