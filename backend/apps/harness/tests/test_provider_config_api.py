"""Tests for ProviderConfig REST endpoints (org-scoped + workspace aliases).

Covers: GET/PUT/DELETE happy paths, empty api_key on create -> 400,
update without api_key keeps existing key, owner scoping on workspace
alias (foreign workspace -> 404), unknown org / no membership -> 404,
missing org header -> 401, key permissions (harness:read for GET,
harness:run for PUT/DELETE), api_key_hint when key saved, and never
leaking the plaintext API key in responses.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.models import ProviderConfig
from apps.harness.services import ProviderConfigService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import decrypt_value, generate_api_token, hash_token


@pytest.fixture
def provider_setup(db):
    """Org + owner/stranger + runner + owned/foreign workspaces."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Prov API {uuid.uuid4().hex[:6]}",
        slug=f"prov-api-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"p-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"p-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    outsider = user_model.objects.create_user(
        email=f"p-outsider-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=owner, organization=org, role=MembershipRole.MEMBER
    )
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="prov-api-runner",
        api_token_hash=hash_token(f"prov-api-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"prov-api-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    owned = Workspace.objects.create(
        runner=runner,
        name="Owned",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    foreign = Workspace.objects.create(
        runner=runner,
        name="Foreign",
        status=WorkspaceStatus.RUNNING,
        created_by=stranger,
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "outsider": outsider,
        "owned": owned,
        "foreign": foreign,
    }


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"prov-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
BOTH = READ + RUN

ORG_URL = "/api/v1/provider-config/"
WS_URL = "/api/v1/workspaces/{ws}/provider-config/"


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_crud_roundtrip(provider_setup):
    """PUT creates, GET returns masked config with hint, DELETE removes."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )

    put = client.put(
        ORG_URL,
        data=json.dumps(
            {
                "api_key": "sk-live-1234",
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "model-a",
                "small_model": "model-b",
                "computer_use_model": "model-cu",
            }
        ),
        content_type="application/json",
    )
    assert put.status_code == 200, put.content[:500]
    body = put.json()
    assert body["base_url"] == "https://openrouter.ai/api/v1"
    assert body["default_model"] == "model-a"
    assert body["small_model"] == "model-b"
    assert body["computer_use_model"] == "model-cu"
    assert body["has_api_key"] is True
    assert body["api_key_hint"] == "••••1234"
    assert "sk-live-1234" not in put.content.decode()

    get = client.get(ORG_URL)
    assert get.status_code == 200, get.content[:500]
    assert get.json()["default_model"] == "model-a"
    assert get.json()["api_key_hint"] == "••••1234"
    assert "sk-live-1234" not in get.content.decode()

    deleted = client.delete(ORG_URL)
    assert deleted.status_code == 204

    gone = client.get(ORG_URL)
    assert gone.status_code == 404

    assert (
        ProviderConfig.objects.filter(
            organization_id=provider_setup["org"].id
        ).count()
        == 0
    )


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_update_without_key_keeps_existing(provider_setup):
    """PUT without api_key on an existing config preserves the stored key."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    created = client.put(
        ORG_URL,
        data=json.dumps(
            {
                "api_key": "sk-keep-me-99",
                "default_model": "model-a",
            }
        ),
        content_type="application/json",
    )
    assert created.status_code == 200

    updated = client.put(
        ORG_URL,
        data=json.dumps(
            {
                "api_key": "",
                "default_model": "model-updated",
                "small_model": "model-small",
            }
        ),
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content[:500]
    body = updated.json()
    assert body["default_model"] == "model-updated"
    assert body["small_model"] == "model-small"
    assert body["has_api_key"] is True
    assert body["api_key_hint"] == "••••e-99"

    service = ProviderConfigService()
    assert service.get_decrypted_api_key(provider_setup["org"].id) == "sk-keep-me-99"


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_empty_api_key_on_create_is_400(provider_setup):
    """Empty api_key on first PUT is a validation error."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    response = client.put(
        ORG_URL,
        data=json.dumps({"api_key": "   "}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_get_missing_is_404(provider_setup):
    """GET without a stored config yields 404."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=READ
    )
    response = client.get(ORG_URL)
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_missing_org_header_is_401(provider_setup):
    """Missing X-Organization-Id yields 401."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    response = Client(
        HTTP_X_API_KEY=client.defaults["HTTP_X_API_KEY"],
    ).get(ORG_URL)
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_no_membership_is_404(provider_setup):
    """User not in org yields 404 via require_membership."""
    other_org = Organization.objects.create(
        name=f"Other {uuid.uuid4().hex[:6]}",
        slug=f"other-{uuid.uuid4().hex[:10]}",
    )
    client = _client(
        user=provider_setup["outsider"], org=other_org, permissions=BOTH
    )
    response = client.get(ORG_URL)
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_unknown_org_is_404(provider_setup):
    """Unknown org id yields 404 via require_membership."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    unknown = str(uuid.uuid4())
    response = Client(
        HTTP_X_API_KEY=client.defaults["HTTP_X_API_KEY"],
        HTTP_X_ORGANIZATION_ID=unknown,
    ).get(ORG_URL)
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_permissions(provider_setup):
    """GET needs harness:read; PUT/DELETE need harness:run."""
    read_client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=READ
    )
    denied_put = read_client.put(
        ORG_URL,
        data=json.dumps({"api_key": "sk-x"}),
        content_type="application/json",
    )
    assert denied_put.status_code == 403
    assert denied_put.json()["code"] == "permission_denied"

    denied_delete = read_client.delete(ORG_URL)
    assert denied_delete.status_code == 403

    run_client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=RUN
    )
    denied_get = run_client.get(ORG_URL)
    assert denied_get.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_org_provider_config_unauthenticated_is_401(provider_setup):
    """No credentials yields 401 on provider-config endpoints."""
    client = Client(HTTP_X_ORGANIZATION_ID=str(provider_setup["org"].id))
    response = client.get(ORG_URL)
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_workspace_provider_config_alias_still_works(provider_setup):
    """Workspace-scoped paths remain as owner-gated aliases."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    ws = provider_setup["owned"].id

    put = client.put(
        WS_URL.format(ws=ws),
        data=json.dumps({"api_key": "sk-alias-key"}),
        content_type="application/json",
    )
    assert put.status_code == 200

    get = client.get(WS_URL.format(ws=ws))
    assert get.status_code == 200
    assert get.json()["has_api_key"] is True


@pytest.mark.django_db(transaction=True)
def test_workspace_provider_config_foreign_workspace_is_404(provider_setup):
    """Owner scoping: another user's workspace reads as not found."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    response = client.get(WS_URL.format(ws=provider_setup["foreign"].id))
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_save_config_service_keeps_key_when_omitted(organization) -> None:
    """Service-level: update without api_key preserves the encrypted value."""
    service = ProviderConfigService()
    first = service.save_config(
        organization_id=organization.id,
        api_key="sk-service-keep",
        default_model="m1",
    )
    encrypted_before = first.api_key_encrypted

    second = service.save_config(
        organization_id=organization.id,
        api_key="",
        default_model="m2",
    )
    assert second.api_key_encrypted == encrypted_before
    assert service.get_decrypted_api_key(organization.id) == "sk-service-keep"
    assert decrypt_value(second.api_key_encrypted) == "sk-service-keep"


@pytest.mark.django_db(transaction=True)
def test_org_provider_models_lists_catalog(provider_setup, monkeypatch):
    """GET /provider-config/models/ returns the normalized OpenRouter catalog."""
    from apps.harness.providers.models_catalog import ProviderModel

    ProviderConfigService().save_config(
        organization_id=provider_setup["org"].id,
        api_key="sk-models-key",
        default_model="acme/fast",
    )

    def _fake_list(self, organization_id):  # type: ignore[no-untyped-def]
        assert organization_id == provider_setup["org"].id
        return [
            ProviderModel(
                id="acme/fast",
                name="Fast",
                reasoning_efforts=("high",),
                default_effort="high",
                supports_tools=True,
                context_length=128000,
                max_output_tokens=16384,
            )
        ]

    monkeypatch.setattr(ProviderConfigService, "list_models", _fake_list)
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=READ
    )
    response = client.get("/api/v1/provider-config/models/")
    assert response.status_code == 200, response.content[:500]
    body = response.json()
    assert body[0]["id"] == "acme/fast"
    assert body[0]["reasoning_efforts"] == ["high"]
    assert body[0]["supports_tools"] is True
    assert body[0]["context_length"] == 128000
    assert body[0]["max_output_tokens"] == 16384


@pytest.mark.django_db(transaction=True)
def test_org_provider_models_missing_config_is_404(provider_setup):
    """GET models without a stored config yields 404."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=READ
    )
    response = client.get("/api/v1/provider-config/models/")
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_org_provider_models_requires_harness_read(provider_setup):
    """GET models is denied without harness:read."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=RUN
    )
    response = client.get("/api/v1/provider-config/models/")
    assert response.status_code == 403
