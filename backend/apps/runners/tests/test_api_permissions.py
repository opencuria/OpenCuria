"""Regression tests for API key permission enforcement."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


def _auth_headers(token: str, org_id: str) -> dict[str, str]:
    return {
        "HTTP_X_API_KEY": token,
        "HTTP_X_ORGANIZATION_ID": org_id,
    }


def _create_api_key(*, user, permissions: list[str]) -> str:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return token


@pytest.mark.django_db
def test_legacy_workspace_sessions_endpoint_is_gone(client: Client):
    user_model = get_user_model()
    user = user_model.objects.create_user(email="gone@test.local", password="secret")
    org = Organization.objects.create(name="Gone Org", slug="gone-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="runner",
        api_token_hash=hash_token("runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    token = _create_api_key(user=user, permissions=[APIKeyPermission.WORKSPACES_READ.value])

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/sessions/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_legacy_conversations_endpoint_is_gone(client: Client):
    user_model = get_user_model()
    user = user_model.objects.create_user(email="conv-gone@test.local", password="secret")
    org = Organization.objects.create(name="Conv Gone Org", slug="conv-gone-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(user=user, permissions=[APIKeyPermission.WORKSPACES_READ.value])

    response = client.post(
        "/api/v1/conversations/unread/",
        data=json.dumps({"session_id": "00000000-0000-0000-0000-000000000001"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 404
