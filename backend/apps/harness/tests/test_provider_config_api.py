"""REST tests for org-wide provider model defaults and ChatGPT OAuth."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.models import ProviderConfig
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.providers.chatgpt_oauth import DeviceFlowStart, OAuthTokens
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token


@pytest.fixture
def provider_setup(db):
    """Create one org member and one outsider; no workspace is needed here."""
    users = get_user_model()
    org = Organization.objects.create(
        name=f"Provider API {uuid.uuid4().hex[:6]}",
        slug=f"provider-api-{uuid.uuid4().hex[:10]}",
    )
    owner = users.objects.create_user(
        email=f"provider-owner-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    outsider = users.objects.create_user(
        email=f"provider-outsider-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    return {"org": org, "owner": owner, "outsider": outsider}


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"provider-{uuid.uuid4().hex[:8]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
PROVIDERS = [APIKeyPermission.HARNESS_PROVIDERS.value]
BOTH = READ + RUN
ORG_URL = "/api/v1/provider-config/"
DEFAULT_FIELDS = {
    "default_model",
    "small_model",
    "computer_use_model",
    "default_effort",
    "small_effort",
    "computer_use_effort",
}


@pytest.mark.django_db(transaction=True)
def test_provider_defaults_roundtrip_has_only_six_fields(provider_setup):
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )

    missing = client.get(ORG_URL)
    assert missing.status_code == 200
    assert set(missing.json()) == DEFAULT_FIELDS
    assert all(value == "" for value in missing.json().values())

    put = client.put(
        ORG_URL,
        data=json.dumps(
            {
                "default_model": "model-a",
                "small_model": "model-b",
                "computer_use_model": "model-cu",
                "default_effort": "high",
                "small_effort": "low",
                "computer_use_effort": "medium",
            }
        ),
        content_type="application/json",
    )
    assert put.status_code == 200, put.content[:500]
    assert set(put.json()) == DEFAULT_FIELDS
    assert put.json() == {
        "default_model": "model-a",
        "small_model": "model-b",
        "computer_use_model": "model-cu",
        "default_effort": "high",
        "small_effort": "low",
        "computer_use_effort": "medium",
    }
    assert client.get(ORG_URL).json() == put.json()
    assert ProviderConfig.objects.filter(organization=provider_setup["org"]).exists()


def test_provider_config_schema_has_no_connection_fields():
    """Defaults schemas cannot accept or expose provider credentials/status."""
    from apps.harness.schemas import ProviderConfigIn, ProviderConfigOut

    assert set(ProviderConfigIn.model_fields) == DEFAULT_FIELDS
    assert set(ProviderConfigOut.model_fields) == DEFAULT_FIELDS


@pytest.mark.django_db(transaction=True)
def test_saving_defaults_does_not_mutate_or_delete_provider_accounts(provider_setup):
    org, owner = provider_setup["org"], provider_setup["owner"]
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={
            "label": "Personal OpenRouter",
            "api_key": "sk-account-secret",
            "base_url": "https://router.example/v1",
        },
    )
    original_config = dict(account.config)

    client = _client(user=owner, org=org, permissions=BOTH)
    response = client.put(
        ORG_URL,
        data=json.dumps(
            {
                "default_model": "new-model",
                "small_model": "small",
                "api_key": "sk-ignored",
                "base_url": "https://ignored.example/v1",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200, response.content[:500]
    account.refresh_from_db()
    assert account.id == accounts.selected_accounts(org.id, owner.id)["openrouter"].id
    assert account.config == original_config
    assert accounts.get_credentials(account)["api_key"] == "sk-account-secret"
    assert ProviderConfig.objects.filter(organization=org).exists()

    deleted_defaults = client.delete(ORG_URL)
    assert deleted_defaults.status_code == 204
    account.refresh_from_db()
    assert account.config == original_config
    assert accounts.get_credentials(account)["api_key"] == "sk-account-secret"
    assert not ProviderConfig.objects.filter(organization=org).exists()


@pytest.mark.django_db(transaction=True)
def test_provider_config_permissions_and_membership(provider_setup):
    org, owner, outsider = (
        provider_setup["org"],
        provider_setup["owner"],
        provider_setup["outsider"],
    )
    read_client = _client(user=owner, org=org, permissions=READ)
    run_client = _client(user=owner, org=org, permissions=RUN)
    denied_put = read_client.put(
        ORG_URL,
        data=json.dumps({"default_model": "x"}),
        content_type="application/json",
    )
    assert denied_put.status_code == 403
    assert denied_put.json()["code"] == "permission_denied"
    assert run_client.get(ORG_URL).status_code == 403

    outsider_client = _client(user=outsider, org=org, permissions=BOTH)
    assert outsider_client.get(ORG_URL).status_code == 404
    assert Client().get(ORG_URL).status_code == 401


@pytest.mark.django_db(transaction=True)
def test_provider_models_use_the_callers_selected_account(provider_setup, monkeypatch):
    from apps.harness.providers.models_catalog import ProviderModel

    org, owner = provider_setup["org"], provider_setup["owner"]
    ProviderAccountService().save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={"label": "Router", "api_key": "sk-models"},
    )

    def fake_list(*, organization_id, **kwargs):  # type: ignore[no-untyped-def]
        assert organization_id == org.id
        assert kwargs["user_id"] == owner.id
        return [
            ProviderModel(
                id="openrouter/acme/fast",
                name="Fast",
                provider="openrouter",
                reasoning_efforts=("high",),
                default_effort="high",
                supports_tools=True,
                context_length=128000,
                max_output_tokens=16384,
            )
        ]

    monkeypatch.setattr(
        "apps.harness.engines.service.list_merged_provider_models", fake_list
    )
    client = _client(user=owner, org=org, permissions=READ)
    response = client.get("/api/v1/harness/engines/native/models/")
    assert response.status_code == 200, response.content[:500]
    assert response.json()[0]["id"] == "openrouter/acme/fast"
    assert response.json()[0]["provider"] == "openrouter"
    assert response.json()[0]["reasoning_efforts"] == ["high"]
    assert client.get("/api/v1/provider-config/models/").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_chatgpt_oauth_start_status_and_cancel(provider_setup):
    """OAuth flow IDs survive polling/cancel, and returned account metadata is safe."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=PROVIDERS
    )
    base = "/api/v1/provider-config/providers/chatgpt/oauth/"

    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
    ) as mock_start:
        mock_start.return_value = DeviceFlowStart(
            device_auth_id="auth-1",
            user_code="ABCD-1234",
            verification_url="https://auth.openai.com/codex/device",
            interval=5,
        )
        start = client.post(f"{base}start/")
    assert start.status_code == 200, start.content[:500]
    body = start.json()
    assert body["user_code"] == "ABCD-1234"
    assert body["expires_in"] == 600
    flow_id = body["flow_id"]

    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
    ) as mock_poll:
        from apps.harness.providers.chatgpt_oauth import DeviceFlowPollResult

        mock_poll.return_value = DeviceFlowPollResult(status="pending")
        pending = client.get(f"{base}status/?flow_id={flow_id}")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
    ) as mock_poll:
        from apps.harness.providers.chatgpt_oauth import DeviceFlowPollResult

        mock_poll.return_value = DeviceFlowPollResult(
            status="complete",
            tokens=OAuthTokens(
                access="access-token",
                refresh="refresh-token",
                expires=9999999999,
                account_id="acc-42",
                residency=None,
            ),
        )
        complete = client.get(f"{base}status/?flow_id={flow_id}")
    assert complete.status_code == 200
    assert complete.json() == {"status": "connected", "account_id": "acc-42"}

    rows = ProviderAccountService().list_accounts(
        provider_setup["org"].id, provider_setup["owner"].id, "chatgpt"
    )
    assert len(rows) == 1
    assert rows[0]["account_id"] == "acc-42"
    assert "access-token" not in repr(rows)

    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
    ) as mock_start:
        mock_start.return_value = DeviceFlowStart(
            device_auth_id="auth-2",
            user_code="WXYZ-5678",
            verification_url="https://auth.openai.com/codex/device",
            interval=5,
        )
        cancel_flow_id = client.post(f"{base}start/").json()["flow_id"]
    cancelled = client.post(
        f"{base}cancel/",
        data=json.dumps({"flow_id": cancel_flow_id}),
        content_type="application/json",
    )
    assert cancelled.status_code == 204
    status = client.get(f"{base}status/?flow_id={cancel_flow_id}")
    assert status.json()["status"] == "cancelled"
    assert client.get(f"{base}status/").json()["code"] == "no_flow"
