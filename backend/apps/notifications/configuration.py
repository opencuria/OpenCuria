"""Instance-wide push settings supplied exclusively by the hosting environment."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class APNsConfig(BaseModel):
    """Apple token-signing configuration, never exposed to clients."""

    model_config = ConfigDict(frozen=True)

    team_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    key_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    private_key: SecretStr


class FCMConfig(BaseModel):
    """Service-account fields used by the fixed Google OAuth flow."""

    model_config = ConfigDict(frozen=True)

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
    client_email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.iam\.gserviceaccount\.com$")
    private_key: SecretStr


@dataclass(frozen=True)
class PushSettings:
    """One provider configuration shared by every organization on this server."""

    apns: APNsConfig | None
    fcm: FCMConfig | None

    def capabilities(self) -> dict[str, bool]:
        """Expose availability without provider account details or secrets."""
        return {
            "ios": self.apns is not None,
            "android": self.fcm is not None,
            "live_activities": self.apns is not None,
        }


def get_push_settings() -> PushSettings:
    """Load hosting key files once at startup; unset providers stay disabled."""
    return _load_settings(
        settings.APNS_TEAM_ID,
        settings.APNS_KEY_ID,
        settings.APNS_PRIVATE_KEY_FILE,
        settings.FCM_SERVICE_ACCOUNT_FILE,
    )


@lru_cache(maxsize=1)
def _load_settings(
    team_id: str, key_id: str, apns_key_file: str, fcm_account_file: str
) -> PushSettings:
    apns = None
    if any((team_id, key_id, apns_key_file)):
        if not all((team_id, key_id, apns_key_file)):
            raise ImproperlyConfigured(
                "APNS_TEAM_ID, APNS_KEY_ID and APNS_PRIVATE_KEY_FILE "
                "must be set together"
            )
        private_key = _read_secret_file(apns_key_file, "APNS_PRIVATE_KEY_FILE")
        try:
            apns = APNsConfig(team_id=team_id, key_id=key_id, private_key=private_key)
            _validate_key(apns)
        except Exception:
            raise ImproperlyConfigured(
                "Invalid APNs configuration: check APNS_TEAM_ID, APNS_KEY_ID "
                "and APNS_PRIVATE_KEY_FILE"
            ) from None

    fcm = None
    if fcm_account_file:
        account_json = _read_secret_file(fcm_account_file, "FCM_SERVICE_ACCOUNT_FILE")
        try:
            fcm = FCMConfig.model_validate_json(account_json)
            _validate_key(fcm)
        except Exception:
            raise ImproperlyConfigured(
                "Invalid FCM_SERVICE_ACCOUNT_FILE JSON or private key"
            ) from None

    return PushSettings(apns=apns, fcm=fcm)


def _read_secret_file(path: str, name: str) -> str:
    if not Path(path).is_absolute():
        raise ImproperlyConfigured(f"{name} must be an absolute file path")
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ImproperlyConfigured(
            f"{name} must point to a readable UTF-8 file"
        ) from None


def _validate_key(config: APNsConfig | FCMConfig) -> None:
    key = serialization.load_pem_private_key(
        config.private_key.get_secret_value().encode(), password=None
    )
    if isinstance(config, APNsConfig):
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("APNs requires a P-256 EC key")
    elif not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("FCM requires an RSA key")
