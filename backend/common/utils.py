"""
Shared utility functions for opencuria backend.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid

from cryptography.fernet import Fernet


def generate_uuid() -> uuid.UUID:
    """Generate a new UUID4."""
    return uuid.uuid4()


def generate_api_token() -> str:
    """Generate a cryptographically secure API token."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Hash an API token for secure storage using SHA-256."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    """Verify a plaintext token against its hash."""
    return secrets.compare_digest(hash_token(token), token_hash)


# ---------------------------------------------------------------------------
# Credential encryption (Fernet)
# ---------------------------------------------------------------------------

def _get_fernet() -> Fernet:
    """Return a Fernet instance using the configured encryption key.

    The key is read from the ``CREDENTIAL_ENCRYPTION_KEY`` environment
    variable. If not set, a deterministic key is derived from
    ``DJANGO_SECRET_KEY`` for development convenience (NOT recommended
    for production).
    """
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if key:
        return Fernet(key.encode())

    # Fallback for development: derive a Fernet-compatible key from the
    # Django secret key via SHA-256 → base64url (32 bytes).
    import base64

    from django.conf import settings

    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(digest)
    return Fernet(fernet_key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string and return the Fernet ciphertext as UTF-8."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string and return the plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# SSH key generation
# ---------------------------------------------------------------------------


def generate_ssh_keypair() -> tuple[str, str]:
    """Generate an Ed25519 SSH keypair.

    Returns:
        A tuple of ``(private_key_pem, public_key_openssh)`` where
        ``private_key_pem`` is the PEM-encoded private key and
        ``public_key_openssh`` is the public key in OpenSSH format.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.OpenSSH,
        encryption_algorithm=NoEncryption(),
    ).decode()
    public_openssh = private_key.public_key().public_bytes(
        encoding=Encoding.OpenSSH,
        format=PublicFormat.OpenSSH,
    ).decode()

    return private_pem, public_openssh
