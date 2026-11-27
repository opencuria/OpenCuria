"""Settings from the environment; invalid credentials stop the proxy at startup."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

HANDLE_KEY_BYTES = 64  # AES-256-SIV
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_APPLE_ID = re.compile(r"^[A-Z0-9]{10}$")


class ConfigError(Exception):
    """A setting is missing or invalid; the message never contains secret values."""


class _ServiceAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
    client_email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.iam\.gserviceaccount\.com$")
    private_key: str


@dataclass(frozen=True)
class APNsCredentials:
    """Token-based APNs signing key and the bundle IDs it may push to."""

    team_id: str
    key_id: str
    private_key: str
    app_ids: frozenset[str]


@dataclass(frozen=True)
class FCMCredentials:
    """Firebase service account and the Android packages it may push to."""

    project_id: str
    client_email: str
    private_key: str
    app_ids: frozenset[str]


@dataclass(frozen=True)
class Settings:
    """Everything the proxy needs; it keeps no other state."""

    public_url: str
    handle_keys: tuple[bytes, ...]
    apns: APNsCredentials | None
    fcm: FCMCredentials | None

    def app_ids(self, platform: str) -> frozenset[str]:
        """Apps this proxy holds credentials for on a platform."""
        credentials = self.apns if platform == "ios" else self.fcm
        return credentials.app_ids if credentials else frozenset()


def load_settings(env: Mapping[str, str]) -> Settings:
    """Read and validate all settings once at startup."""
    settings = Settings(
        public_url=_public_url(env.get("PUSH_PROXY_PUBLIC_URL", "")),
        handle_keys=_handle_keys(env.get("PUSH_PROXY_HANDLE_KEYS", "")),
        apns=_apns(env),
        fcm=_fcm(env),
    )
    if settings.apns is None and settings.fcm is None:
        raise ConfigError("Configure APNs (iOS) and/or FCM (Android) credentials")
    return settings


def _public_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = -1
    loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parts.scheme not in {"https", "http"}
        or (parts.scheme == "http" and not loopback)
        or not parts.hostname
        or port == -1
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
    ):
        raise ConfigError(
            "PUSH_PROXY_PUBLIC_URL must be the proxy's https:// origin, "
            "e.g. https://push.example.com"
        )
    # Servers normalize the VAPID audience the same way: lower case, without a
    # default port.
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    if port is not None and port != {"https": 443, "http": 80}[parts.scheme]:
        host += f":{port}"
    return f"{parts.scheme}://{host}"


def _handle_keys(value: str) -> tuple[bytes, ...]:
    keys = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        try:
            key = base64.b64decode(
                item.replace("-", "+").replace("_", "/") + "=" * (-len(item) % 4),
                validate=True,
            )
        except (binascii.Error, ValueError):
            key = b""
        if len(key) != HANDLE_KEY_BYTES:
            raise ConfigError(
                "PUSH_PROXY_HANDLE_KEYS must be comma-separated base64 keys of "
                f"{HANDLE_KEY_BYTES} bytes (openssl rand -base64 {HANDLE_KEY_BYTES})"
            )
        keys.append(key)
    if not keys:
        raise ConfigError("PUSH_PROXY_HANDLE_KEYS is required")
    return tuple(keys)


def _app_ids(value: str, name: str) -> frozenset[str]:
    ids = frozenset(filter(None, (part.strip() for part in value.split(","))))
    if not ids or not all(_APP_ID.match(app_id) for app_id in ids):
        raise ConfigError(f"{name} must list the app's bundle IDs, comma-separated")
    return ids


def _apns(env: Mapping[str, str]) -> APNsCredentials | None:
    names = ("APNS_TEAM_ID", "APNS_KEY_ID", "APNS_PRIVATE_KEY_FILE", "APNS_APP_IDS")
    values = [env.get(name, "").strip() for name in names]
    if not any(values):
        return None
    if not all(values):
        raise ConfigError(f"{', '.join(names)} must be set together")
    team_id, key_id, key_file, app_ids = values
    if not _APPLE_ID.match(team_id) or not _APPLE_ID.match(key_id):
        raise ConfigError("APNS_TEAM_ID and APNS_KEY_ID are 10-character Apple IDs")
    private_key = _read_file(key_file, "APNS_PRIVATE_KEY_FILE")
    if not _is_key(private_key, ec.EllipticCurvePrivateKey):
        raise ConfigError("APNS_PRIVATE_KEY_FILE must contain the P-256 .p8 key")
    return APNsCredentials(
        team_id=team_id,
        key_id=key_id,
        private_key=private_key,
        app_ids=_app_ids(app_ids, "APNS_APP_IDS"),
    )


def _fcm(env: Mapping[str, str]) -> FCMCredentials | None:
    account_file = env.get("FCM_SERVICE_ACCOUNT_FILE", "").strip()
    app_ids = env.get("FCM_APP_IDS", "").strip()
    if not account_file and not app_ids:
        return None
    if not account_file or not app_ids:
        raise ConfigError(
            "FCM_SERVICE_ACCOUNT_FILE and FCM_APP_IDS must be set together"
        )
    try:
        account = _ServiceAccount.model_validate_json(
            _read_file(account_file, "FCM_SERVICE_ACCOUNT_FILE")
        )
    except ValidationError:
        account = None
    if account is None or not _is_key(account.private_key, rsa.RSAPrivateKey):
        raise ConfigError("FCM_SERVICE_ACCOUNT_FILE must be a Firebase service account")
    return FCMCredentials(
        project_id=account.project_id,
        client_email=account.client_email,
        private_key=account.private_key,
        app_ids=_app_ids(app_ids, "FCM_APP_IDS"),
    )


def _read_file(path: str, name: str) -> str:
    if not Path(path).is_absolute():
        raise ConfigError(f"{name} must be an absolute file path")
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigError(f"{name} must point to a readable UTF-8 file") from None


def _is_key(pem: str, kind: type) -> bool:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return False
    if kind is ec.EllipticCurvePrivateKey:
        return isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(
            key.curve, ec.SECP256R1
        )
    return isinstance(key, kind)
