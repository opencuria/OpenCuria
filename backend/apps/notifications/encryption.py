"""Web Push message encryption (RFC 8291, aes128gcm) for one device key.

Only the device holds the private key, so neither the push proxy nor Apple or
Google can read notification content. Plaintext is padded to a fixed size so
ciphertext length does not reveal titles either.
"""

from __future__ import annotations

import base64
import os
import re

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
ALERT_PADDED_SIZE = 1024
ACTIVITY_PADDED_SIZE = 2048
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def decode_b64url(value: str) -> bytes:
    """Strict unpadded base64url."""
    if not _BASE64URL.match(value) or len(value) % 4 == 1:
        raise ValueError("Invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_receiver_key(value: str) -> bytes:
    """Return a device's uncompressed P-256 public key (65 bytes)."""
    raw = decode_b64url(value)
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("The encryption key must be an uncompressed P-256 point")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    return raw


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(
        ikm
    )


def encrypt(
    plaintext: bytes, *, receiver_key: str, auth_secret: str, padded_size: int
) -> str:
    """Encrypt for a device and return the base64url aes128gcm body."""
    body = encrypt_with(
        plaintext,
        receiver_key=validate_receiver_key(receiver_key),
        auth_secret=decode_b64url(auth_secret),
        padded_size=padded_size,
        sender_key=ec.generate_private_key(ec.SECP256R1()),
        salt=os.urandom(16),
    )
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")


def encrypt_with(
    plaintext: bytes,
    *,
    receiver_key: bytes,
    auth_secret: bytes,
    padded_size: int,
    sender_key: ec.EllipticCurvePrivateKey,
    salt: bytes,
) -> bytes:
    """RFC 8291 section 3.4 with explicit randomness (tests use RFC vectors)."""
    if len(auth_secret) != 16 or len(salt) != 16:
        raise ValueError("auth_secret and salt are 16 bytes")
    if len(plaintext) > padded_size or padded_size + 17 > RECORD_SIZE:
        raise ValueError("The message does not fit the padded record")
    sender_public = sender_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), receiver_key
    )
    shared = sender_key.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(
        auth_secret, shared, b"WebPush: info\x00" + receiver_key + sender_public, 32
    )
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    # One final record: plaintext, the 0x02 delimiter, then zero padding.
    record = plaintext + b"\x02" + b"\x00" * (padded_size - len(plaintext))
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([65]) + sender_public
    return header + AESGCM(key).encrypt(nonce, record, None)
