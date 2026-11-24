"""Local keys, settings and a mock provider network; no Apple or Google calls."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from starlette.testclient import TestClient

from push_proxy.app import create_app
from push_proxy.config import Settings, load_settings
from push_proxy.encoding import b64url_encode

PUBLIC_URL = "https://push.example.test"
IOS_APP = "com.opencuria.mobile"
ANDROID_APP = "com.opencuria.mobile"
APNS_TOKEN = "ab" * 32
FCM_TOKEN = "fcm:token-" + "x" * 140


def _pem(key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def provider_keys() -> dict[str, str]:
    """Throwaway signing keys standing in for the .p8 and the service account."""
    return {
        "apns": _pem(ec.generate_private_key(ec.SECP256R1())),
        "fcm": _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
    }


@pytest.fixture
def env(tmp_path, provider_keys) -> dict[str, str]:
    """A complete environment for a proxy serving iOS and Android."""
    apns_key = tmp_path / "apns.p8"
    apns_key.write_text(provider_keys["apns"], encoding="utf-8")
    account = tmp_path / "firebase.json"
    account.write_text(
        json.dumps(
            {
                "project_id": "opencuria-test",
                "client_email": "push@opencuria-test.iam.gserviceaccount.com",
                "private_key": provider_keys["fcm"],
            }
        ),
        encoding="utf-8",
    )
    return {
        "PUSH_PROXY_PUBLIC_URL": PUBLIC_URL,
        "PUSH_PROXY_HANDLE_KEYS": base64.b64encode(os.urandom(64)).decode(),
        "APNS_TEAM_ID": "TEAM123456",
        "APNS_KEY_ID": "KEY1234567",
        "APNS_PRIVATE_KEY_FILE": str(apns_key),
        "APNS_APP_IDS": f"{IOS_APP},{IOS_APP}.dev",
        "FCM_SERVICE_ACCOUNT_FILE": str(account),
        "FCM_APP_IDS": ANDROID_APP,
    }


@pytest.fixture
def settings(env) -> Settings:
    return load_settings(env)


@dataclass
class Backend:
    """A backend identity as OpenCuria derives it: a P-256 VAPID key."""

    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def server_key(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return b64url_encode(raw)

    def authorization(
        self, audience: str = PUBLIC_URL, expires_in: int = 3600, key=None
    ) -> str:
        token = jwt.encode(
            {"aud": audience, "exp": int(time.time()) + expires_in},
            (key or self.private_key),
            algorithm="ES256",
        )
        return f"vapid t={token}, k={self.server_key}"


@pytest.fixture
def backend() -> Backend:
    return Backend()


def ciphertext(size: int = 200) -> str:
    """Bytes shaped like one aes128gcm record (the proxy cannot decrypt them)."""
    sender = os.urandom(64)
    header = os.urandom(16) + (4096).to_bytes(4, "big") + bytes([65, 4]) + sender
    return b64url_encode(header + os.urandom(max(17, size - len(header))))


@dataclass
class Network:
    """Records provider requests and answers with configurable responses."""

    requests: list[httpx.Request] = field(default_factory=list)
    apns: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(
        200
    )
    fcm: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(
        200, json={"name": "projects/opencuria-test/messages/1"}
    )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "oauth", "expires_in": 3600}
            )
        if request.url.host == "fcm.googleapis.com":
            return self.fcm(request)
        return self.apns(request)

    @property
    def deliveries(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host != "oauth2.googleapis.com"]


@pytest.fixture
def network() -> Network:
    return Network()


@pytest.fixture
def client(settings, network) -> Iterator[TestClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(network.handle))
    with TestClient(create_app(settings, client=http)) as test_client:
        yield test_client
