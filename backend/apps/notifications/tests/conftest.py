"""Disposable organizations, devices and device keys for native delivery tests."""

import base64
import json
import os
import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.accounts.models import User
from apps.harness.models import HarnessMessage, HarnessSession
from apps.organizations.models import Membership, Organization
from apps.runners.models import Runner, Workspace

from ..schemas import DeviceRegistrationIn
from ..services import NotificationService

PROXY_URL = "https://push.example.test"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class DeviceKeys:
    """What the app keeps in its keychain: an RFC 8291 key pair and auth secret."""

    private_key: ec.EllipticCurvePrivateKey
    auth: bytes

    @property
    def public(self) -> str:
        return b64url(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )

    @property
    def auth_secret(self) -> str:
        return b64url(self.auth)

    def decrypt(self, payload: str) -> bytes:
        """Decrypt like the app's extensions do; fails for any other receiver."""
        body = unb64url(payload)
        salt, idlen = body[:16], body[20]
        sender = body[21 : 21 + idlen]
        ciphertext = body[21 + idlen :]
        shared = self.private_key.exchange(
            ec.ECDH(),
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender),
        )

        def hkdf(salt, ikm, info, length):
            return HKDF(SHA256(), length, salt, info).derive(ikm)

        receiver = unb64url(self.public)
        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + receiver + sender, 32)
        key = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        record = AESGCM(key).decrypt(nonce, ciphertext, None).rstrip(b"\x00")
        assert record.endswith(b"\x02")
        return record[:-1]

    def open(self, payload: str) -> dict:
        return json.loads(self.decrypt(payload))


@pytest.fixture
def device_keys() -> DeviceKeys:
    return DeviceKeys(ec.generate_private_key(ec.SECP256R1()), os.urandom(16))


@pytest.fixture
def push_settings(settings):
    """Route mobile push through a (mocked) push proxy."""
    settings.PUSH_PROXY_URL = PROXY_URL
    return settings


def registration(keys: DeviceKeys, **overrides) -> DeviceRegistrationIn:
    """An iOS installation that opted into notifications and Live Activities."""
    fields = {
        "platform": "ios",
        "push_handle": "alert-handle",
        "encryption_key": keys.public,
        "auth_secret": keys.auth_secret,
        "notifications_enabled": True,
        "live_activities_enabled": True,
        **overrides,
    }
    return DeviceRegistrationIn(**fields)


@pytest.fixture
def setup(db, push_settings):
    """Create the same owner/membership boundary used by workspace REST."""
    org = Organization.objects.create(name="Push test", slug=f"push-{uuid.uuid4().hex}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", password="test"
    )
    Membership.objects.create(user=user, organization=org, role="admin")
    runner = Runner.objects.create(
        name="Push runner", organization=org, api_token_hash="test"
    )
    workspace = Workspace.objects.create(
        name="Push workspace", runner=runner, created_by=user, status="running"
    )
    session = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=org.id,
        created_by=user,
        title="Build feature",
        status="busy",
    )
    message = HarnessMessage.objects.create(session=session, role="assistant")
    return org, user, workspace, session, message


@pytest.fixture
def headers(setup):
    org, user, *_ = setup
    token = DjangoJWTBackend().generate_tokens(user).access_token
    return {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_X_ORGANIZATION_ID": str(org.id),
    }


@pytest.fixture
def device(setup, device_keys):
    org, user, *_ = setup
    return NotificationService().register(
        user, org.id, uuid.uuid4(), registration(device_keys)
    )
