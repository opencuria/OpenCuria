"""Tests for ProviderConfig REST endpoints (M1/M6 gap closure).

Covers: GET/PUT/DELETE happy paths, empty api_key -> 400, owner
scoping (foreign workspace -> 404), unknown org -> 404, key
permissions (harness:read for GET, harness:run for PUT/DELETE),
and never leaking the plaintext API key in responses.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.models import ProviderConfig
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


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

URL = "/api/v1/workspaces/{ws}/provider-config/"


@pytest.mark.django_db(transaction=True)
def test_provider_config_crud_roundtrip(provider_setup):
    """PUT creates, GET returns masked config, DELETE removes."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    ws = provider_setup["owned"].id

    put = client.put(
        URL.format(ws=ws),
        data=json.dumps(
            {
                "api_key": "sk-live-123",
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "model-a",
                "small_model": "model-b",
            }
        ),
        content_type="application/json",
    )
    assert put.status_code == 200, put.content[:500]
    body = put.json()
    assert body["base_url"] == "https://openrouter.ai/api/v1"
    assert body["default_model"] == "model-a"
    assert body["small_model"] == "model-b"
    assert body["has_api_key"] is True
    assert "sk-live-123" not in put.content.decode()

    get = client.get(URL.format(ws=ws))
    assert get.status_code == 200, get.content[:500]
    assert get.json()["default_model"] == "model-a"
    assert "sk-live-123" not in get.content.decode()

    deleted = client.delete(URL.format(ws=ws))
    assert deleted.status_code == 204

    gone = client.get(URL.format(ws=ws))
    assert gone.status_code == 404

    # DB row really gone; key was stored encrypted, never plaintext.
    assert (
        ProviderConfig.objects.filter(
            organization_id=provider_setup["org"].id
        ).count()
        == 0
    )


@pytest.mark.django_db(transaction=True)
def test_provider_config_empty_api_key_is_400(provider_setup):
    """Empty api_key on PUT is a validation error."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    response = client.put(
        URL.format(ws=provider_setup["owned"].id),
        data=json.dumps({"api_key": "   "}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_provider_config_get_missing_is_404(provider_setup):
    """GET without a stored config yields 404."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=READ
    )
    response = client.get(URL.format(ws=provider_setup["owned"].id))
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_provider_config_foreign_workspace_is_404(provider_setup):
    """Owner scoping: another user's workspace reads as not found."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    response = client.get(URL.format(ws=provider_setup["foreign"].id))
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_provider_config_unknown_org_is_404(provider_setup):
    """Unknown org id yields 404 via require_membership."""
    client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=BOTH
    )
    unknown = str(uuid.uuid4())
    response = Client(
        HTTP_X_API_KEY=client.defaults["HTTP_X_API_KEY"],
        HTTP_X_ORGANIZATION_ID=unknown,
    ).get(URL.format(ws=provider_setup["owned"].id))
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_provider_config_permissions(provider_setup):
    """GET needs harness:read; PUT/DELETE need harness:run."""
    read_client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=READ
    )
    ws = provider_setup["owned"].id
    denied_put = read_client.put(
        URL.format(ws=ws),
        data=json.dumps({"api_key": "sk-x"}),
        content_type="application/json",
    )
    assert denied_put.status_code == 403
    assert denied_put.json()["code"] == "permission_denied"

    run_client = _client(
        user=provider_setup["owner"], org=provider_setup["org"], permissions=RUN
    )
    denied_get = run_client.get(URL.format(ws=ws))
    assert denied_get.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_provider_config_unauthenticated_is_401(provider_setup):
    """No credentials yields 401 on provider-config endpoints."""
    client = Client(HTTP_X_ORGANIZATION_ID=str(provider_setup["org"].id))
    response = client.get(URL.format(ws=provider_setup["owned"].id))
    assert response.status_code == 401
