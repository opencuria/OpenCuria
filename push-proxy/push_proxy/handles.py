"""Opaque push handles: a native token sealed for one backend, readable only here.

Handles are deterministic (AES-SIV), so a backend recognises the same device
across registrations without ever seeing its APNs/FCM token. Each handle names
the backend key that may use it; any other sender is refused.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

from .encoding import b64url_decode, b64url_encode

HANDLE_VERSION = 1
_ASSOCIATED_DATA = [b"opencuria-push-handle", bytes([HANDLE_VERSION])]

Platform = Literal["ios", "android"]
Environment = Literal["production", "sandbox"]
Kind = Literal["alert", "activity"]


@dataclass(frozen=True)
class Destination:
    """Where one handle delivers and which backend may use it."""

    platform: Platform
    app_id: str
    environment: Environment
    kind: Kind
    token: str
    server: str  # server_key_thumbprint() of the bound backend key


_FIELDS = {"platform", "app_id", "environment", "kind", "token", "server"}


class HandleSealer:
    """Seal with the first key; open with any key so keys can be rotated."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise ValueError("At least one handle key is required")
        self._ciphers = [AESSIV(key) for key in keys]

    def seal(self, destination: Destination) -> str:
        """Return the handle for a destination (same input, same handle)."""
        plaintext = json.dumps(
            asdict(destination), sort_keys=True, separators=(",", ":")
        ).encode()
        sealed = self._ciphers[0].encrypt(plaintext, _ASSOCIATED_DATA)
        return b64url_encode(bytes([HANDLE_VERSION]) + sealed)

    def open(self, handle: str) -> Destination | None:
        """Return the destination; None for foreign, rotated-out or forged handles."""
        try:
            raw = b64url_decode(handle)
        except ValueError:
            return None
        if len(raw) < 17 or raw[0] != HANDLE_VERSION:
            return None
        for cipher in self._ciphers:
            try:
                plaintext = cipher.decrypt(raw[1:], _ASSOCIATED_DATA)
            except InvalidTag:
                continue
            try:
                fields = json.loads(plaintext)
                if not isinstance(fields, dict) or set(fields) != _FIELDS:
                    return None
                return Destination(**fields)
            except (ValueError, TypeError):
                return None
        return None
