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

_FERNET_KEY_GENERATION_HINT = (
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _is_placeholder_encryption_key(key: str) -> bool:
    """Return whether *key* is an unconfigured template value."""
    return key.startswith("CHANGE_ME")


def _fernet_from_secret_key() -> Fernet:
    """Derive a Fernet instance from ``DJANGO_SECRET_KEY``."""
    import base64

    from django.conf import settings

    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(digest)
    return Fernet(fernet_key)


def _get_fernet() -> Fernet:
    """Return a Fernet instance using the configured encryption key.

    The key is read from the ``CREDENTIAL_ENCRYPTION_KEY`` environment
    variable. Placeholder values (``CHANGE_ME*``) and unset values fall
    back to a deterministic key derived from ``DJANGO_SECRET_KEY`` for
    development convenience (NOT recommended for production).
    """
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if key and not _is_placeholder_encryption_key(key):
        try:
            return Fernet(key.encode())
        except ValueError as exc:
            raise ValueError(
                "CREDENTIAL_ENCRYPTION_KEY is set but is not a valid "
                f"Fernet key. {_FERNET_KEY_GENERATION_HINT}"
            ) from exc

    return _fernet_from_secret_key()


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
