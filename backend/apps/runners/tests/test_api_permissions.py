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
def test_api_key_without_conversations_read_cannot_list_workspace_sessions(client: Client):
    user_model = get_user_model()
    user = user_model.objects.create_user(email="conv@test.local", password="secret")
    org = Organization.objects.create(name="Conv Org", slug="conv-org")
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

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_api_key_without_conversations_read_cannot_mark_conversation_unread(client: Client):
    user_model = get_user_model()
    user = user_model.objects.create_user(email="conv-unread@test.local", password="secret")
    org = Organization.objects.create(name="Conv Unread Org", slug="conv-unread-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(user=user, permissions=[APIKeyPermission.WORKSPACES_READ.value])

    response = client.post(
        "/api/v1/conversations/unread/",
        data=json.dumps({"session_id": "00000000-0000-0000-0000-000000000001"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_api_key_without_org_agent_write_cannot_create_credential_relation(client: Client):
    user_model = get_user_model()
    user = user_model.objects.create_user(email="agent@test.local", password="secret")
    org = Organization.objects.create(name="Agent Org", slug="agent-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(user=user, permissions=[APIKeyPermission.ORG_AGENT_DEFINITIONS_READ.value])

    from apps.credentials.models import CredentialService
    from apps.runners.models import AgentDefinition

    agent = AgentDefinition.objects.create(
        name="Org Agent",
        organization=org,
    )
    service = CredentialService.objects.create(
        name="GitHub Token",
        slug="github-token-perm-test",
        credential_type="env",
        env_var_name="GITHUB_TOKEN",
        label="GitHub PAT",
    )

    response = client.post(
        f"/api/v1/org-agent-definitions/{agent.id}/credential-relations/",
        data=json.dumps(
            {
                "credential_service_id": str(service.id),
                "default_env": {},
                "commands": [],
            }
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
