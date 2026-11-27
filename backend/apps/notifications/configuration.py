"""Push proxy endpoint and this server's VAPID identity.

Mobile push leaves this server only through the push proxy (PUSH_PROXY_URL);
the backend holds no APNs or FCM credentials. The identity key is derived
from DJANGO_SECRET_KEY, so API and execution processes share it without
extra state; rotating the secret key makes devices register again.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# Order of the P-256 group.
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_IDENTITY_INFO = b"opencuria push proxy vapid key v1"
VAPID_TOKEN_LIFETIME = 12 * 60 * 60


def b64url(data: bytes) -> str:
    """Unpadded base64url, the encoding of every key on the proxy protocol."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class ServerIdentity:
    """The key the proxy binds this server's device handles to."""

    private_key: ec.EllipticCurvePrivateKey
    public_key: str

    def authorization(self, audience: str, now: int | None = None) -> tuple[str, int]:
        """Return a VAPID (RFC 8292) Authorization header and its expiry."""
        expires = int(time.time() if now is None else now) + VAPID_TOKEN_LIFETIME
        token = jwt.encode(
            {"aud": audience, "exp": expires}, self.private_key, algorithm="ES256"
        )
        return f"vapid t={token}, k={self.public_key}", expires


@dataclass(frozen=True)
class PushSettings:
    """Where pushes go; `proxy_url` None turns mobile push off."""

    proxy_url: str | None
    identity: ServerIdentity

    @property
    def enabled(self) -> bool:
        """Whether devices may register for notifications."""
        return self.proxy_url is not None

    @property
    def audience(self) -> str:
        """The proxy origin VAPID tokens are issued for."""
        return self.proxy_url or ""

    def capabilities(self) -> dict[str, str | None]:
        """What the app needs to register with the same proxy for this server."""
        return {
            "proxy_url": self.proxy_url,
            "server_key": self.identity.public_key if self.enabled else None,
        }


def get_push_settings() -> PushSettings:
    """Validate the proxy URL and derive the identity once per process."""
    return _load(settings.PUSH_PROXY_URL, settings.SECRET_KEY)


@lru_cache(maxsize=4)
def _load(proxy_url: str, secret_key: str) -> PushSettings:
    return PushSettings(
        proxy_url=_proxy_url(proxy_url), identity=_derive_identity(secret_key)
    )


def _proxy_url(value: str) -> str | None:
    value = value.strip().rstrip("/")
    if not value:
        return None
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
        raise ImproperlyConfigured(
            "PUSH_PROXY_URL must be the push proxy's https:// origin, "
            "e.g. https://push.opencuria.dev, or empty to turn mobile push off"
        )
    # The proxy compares the VAPID audience with its own origin: lower case,
    # without a default port (as the app and the proxy normalize it).
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    if port is not None and port != {"https": 443, "http": 80}[parts.scheme]:
        host += f":{port}"
    return f"{parts.scheme}://{host}"


def _derive_identity(secret_key: str) -> ServerIdentity:
    # 48 bytes reduced modulo the group order keeps the bias negligible
    # (FIPS 186-5 A.2.1).
    seed = HKDF(
        algorithm=hashes.SHA256(), length=48, salt=None, info=_IDENTITY_INFO
    ).derive(secret_key.encode())
    scalar = int.from_bytes(seed, "big") % (_P256_ORDER - 1) + 1
    private_key = ec.derive_private_key(scalar, ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return ServerIdentity(private_key=private_key, public_key=b64url(public_key))
