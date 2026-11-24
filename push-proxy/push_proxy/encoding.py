"""Strict base64url and P-256 key helpers shared by handles and VAPID."""

from __future__ import annotations

import base64
import hashlib
import re

from cryptography.hazmat.primitives.asymmetric import ec

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]*$")


def b64url_encode(data: bytes) -> str:
    """Encode without padding, as used by Web Push and JWTs."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Decode unpadded base64url; anything else is rejected."""
    if not isinstance(value, str) or not _BASE64URL.match(value) or len(value) % 4 == 1:
        raise ValueError("Invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decode_server_key(value: str) -> bytes:
    """Return a validated, uncompressed P-256 public key (65 bytes)."""
    raw = b64url_decode(value)
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("Server keys are uncompressed P-256 points")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    return raw


def server_key_thumbprint(raw_key: bytes) -> str:
    """Short, stable identifier of a backend; never reversible to the backend URL."""
    return b64url_encode(hashlib.sha256(raw_key).digest())
