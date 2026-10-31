"""Disposable organizations, devices and signing keys for native delivery tests."""

import json
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.accounts.models import User
from apps.harness.models import HarnessMessage, HarnessSession
from apps.organizations.models import Membership, Organization
from apps.runners.models import Runner, Workspace

from ..schemas import DeviceRegistrationIn
from ..services import NotificationService


@pytest.fixture(scope="session")
def push_keys():
    """Generate local test keys; these are never production provider credentials."""

    def pem(key):
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    return {
        "apns": {
            "team_id": "TEAM123456",
            "key_id": "KEY1234567",
            "private_key": pem(ec.generate_private_key(ec.SECP256R1())),
        },
        "fcm": {
            "project_id": "opencuria-test",
            "client_email": "push@opencuria-test.iam.gserviceaccount.com",
            "private_key": pem(
                rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ),
        },
    }


@pytest.fixture
def push_settings(settings, push_keys, tmp_path):
    """Enable both providers exclusively through hosting key files."""
    apns_key = tmp_path / "apns.p8"
    apns_key.write_text(push_keys["apns"]["private_key"], encoding="utf-8")
    fcm_account = tmp_path / "firebase.json"
    fcm_account.write_text(json.dumps(push_keys["fcm"]), encoding="utf-8")
    settings.APNS_TEAM_ID = push_keys["apns"]["team_id"]
    settings.APNS_KEY_ID = push_keys["apns"]["key_id"]
    settings.APNS_PRIVATE_KEY_FILE = str(apns_key)
    settings.FCM_SERVICE_ACCOUNT_FILE = str(fcm_account)
    return settings


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
def device(setup):
    org, user, *_ = setup
    return NotificationService().register(
        user,
        org.id,
        uuid.uuid4(),
        DeviceRegistrationIn(
            platform="ios",
            app_id="com.opencuria.mobile.dev",
            apns_environment="sandbox",
            push_token="ab" * 32,
            notifications_enabled=True,
            live_activities_enabled=True,
        ),
    )
