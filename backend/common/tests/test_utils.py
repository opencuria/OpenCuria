"""Tests for common utility helpers."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from common.utils import decrypt_value, encrypt_value


@pytest.fixture
def test_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a stable Django secret key for deterministic fallback tests."""
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-secret-key-for-fernet-fallback")


def test_encrypt_decrypt_with_valid_explicit_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid explicit Fernet key should round-trip encrypt/decrypt."""
    explicit_key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", explicit_key)

    ciphertext = encrypt_value("secret-token")
    assert decrypt_value(ciphertext) == "secret-token"


def test_encrypt_decrypt_falls_back_for_placeholder_key(
    monkeypatch: pytest.MonkeyPatch,
    test_secret_key: None,
) -> None:
    """Placeholder template keys should use the SECRET_KEY-derived fallback."""
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY",
        "CHANGE_ME_generate_a_fernet_key_for_local_use",
    )

    ciphertext = encrypt_value("secret-token")
    assert decrypt_value(ciphertext) == "secret-token"


def test_encrypt_decrypt_falls_back_when_key_unset(
    monkeypatch: pytest.MonkeyPatch,
    test_secret_key: None,
) -> None:
    """Unset CREDENTIAL_ENCRYPTION_KEY should use the SECRET_KEY fallback."""
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)

    ciphertext = encrypt_value("secret-token")
    assert decrypt_value(ciphertext) == "secret-token"


def test_invalid_non_placeholder_key_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid non-placeholder keys should fail with a helpful message."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "not-a-valid-fernet-key")

    with pytest.raises(ValueError, match="not a valid Fernet key"):
        encrypt_value("secret-token")
