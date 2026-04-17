"""
Tests for the runners REST API.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import SessionStatus, WorkspaceStatus
from apps.runners.models import (
    AgentDefinition,
    Chat,
    OrgAgentDefinitionActivation,
    Runner,
    Session,
    Workspace,
)
from common.utils import generate_api_token, hash_token


@pytest.fixture
def auth_context(db):
    """Create an authenticated org admin context for API-key based tests."""
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"runners-api-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Runners API Org {uuid.uuid4().hex[:6]}",
        slug=f"runners-api-org-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="runners-api-test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[
            APIKeyPermission.RUNNERS_READ.value,
            APIKeyPermission.RUNNERS_CREATE.value,
            APIKeyPermission.WORKSPACES_READ.value,
            APIKeyPermission.AGENTS_READ.value,
        ],
    )
    return {
        "user": user,
        "organization": org,
        "headers": {
            "HTTP_X_API_KEY": token,
            "HTTP_X_ORGANIZATION_ID": str(org.id),
        },
    }


@pytest.fixture
def client(auth_context) -> Client:
    """Django test client authenticated via API key + organization context."""
    return Client(**auth_context["headers"])


@pytest.mark.django_db
class TestListRunners:
    def test_returns_runners(self, client, auth_context):
        """GET /api/v1/runners/ should return all runners."""
        runner = Runner.objects.create(
            name="test-runner",
            api_token_hash=hash_token("runner-token-1"),
            organization=auth_context["organization"],
        )
        response = client.get("/api/v1/runners/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(item["id"] == str(runner.id) for item in data)

    def test_empty_list(self, client):
        """GET /api/v1/runners/ should return empty list."""
        response = client.get("/api/v1/runners/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.django_db
class TestGetRunner:
    def test_found(self, client, auth_context):
        """GET /api/v1/runners/{id}/ should return the runner."""
        runner = Runner.objects.create(
            name="test-runner",
            api_token_hash=hash_token("runner-token-2"),
            organization=auth_context["organization"],
        )
        response = client.get(f"/api/v1/runners/{runner.id}/")
        assert response.status_code == 200
        assert response.json()["id"] == str(runner.id)

    def test_not_found(self, client):
        """GET /api/v1/runners/{id}/ should return 404 for missing runner."""
        response = client.get(f"/api/v1/runners/{uuid.uuid4()}/")
        assert response.status_code == 404


@pytest.mark.django_db
class TestCreateRunner:
    def test_creates_runner_with_token(self, client, auth_context):
        """POST /api/v1/runners/ should create a runner and return API token."""
        response = client.post(
            "/api/v1/runners/",
            data={"name": "my-runner"},
            content_type="application/json",
        )
        assert response.status_code == 201
        data = response.json()
        assert "api_token" in data
        assert data["name"] == "my-runner"
        assert Runner.objects.filter(id=data["id"]).exists()


@pytest.mark.django_db
class TestListWorkspaces:
    def test_returns_workspaces(self, client, auth_context):
        """GET /api/v1/workspaces/ should return workspaces."""
        auth_context["organization"].workspace_auto_stop_timeout_minutes = 30
        auth_context["organization"].save(update_fields=["workspace_auto_stop_timeout_minutes"])
        runner = Runner.objects.create(
            name="workspace-runner",
            api_token_hash=hash_token("runner-token-3"),
            organization=auth_context["organization"],
        )
        Workspace.objects.create(
            runner=runner,
            name="Workspace API Fixture",
            created_by=auth_context["user"],
        )
        response = client.get("/api/v1/workspaces/")
        assert response.status_code == 200
        assert len(response.json()) >= 1
        assert "active_operation" in response.json()[0]
        assert response.json()[0]["auto_stop_timeout_minutes"] == 30
        assert response.json()[0]["last_activity_at"]

    def test_detail_returns_active_operation(self, client, auth_context):
        """GET /api/v1/workspaces/{id}/ should include active_operation."""
        auth_context["organization"].workspace_auto_stop_timeout_minutes = 15
        auth_context["organization"].save(update_fields=["workspace_auto_stop_timeout_minutes"])
        runner = Runner.objects.create(
            name="workspace-detail-runner",
            api_token_hash=hash_token("runner-token-detail"),
            organization=auth_context["organization"],
        )
        workspace = Workspace.objects.create(
            runner=runner,
            name="Workspace Detail Fixture",
            status=WorkspaceStatus.RUNNING,
            active_operation="restarting",
            created_by=auth_context["user"],
        )
        response = client.get(f"/api/v1/workspaces/{workspace.id}/")
        assert response.status_code == 200
        assert response.json()["active_operation"] == "restarting"
        assert response.json()["auto_stop_timeout_minutes"] == 15
        assert response.json()["last_activity_at"]
        assert "sessions" not in response.json()


@pytest.mark.django_db
class TestListAgents:
    def test_returns_agents(self, client, auth_context):
        """GET /api/v1/agents/ should return available agents."""
        agent, _ = AgentDefinition.objects.get_or_create(
            name=f"copilot-{uuid.uuid4().hex[:6]}",
            organization=auth_context["organization"],
            defaults={"description": "Copilot test agent"},
        )
        OrgAgentDefinitionActivation.objects.get_or_create(
            organization=auth_context["organization"],
            agent_definition=agent,
        )
        response = client.get("/api/v1/agents/")
        assert response.status_code == 200
        agents = response.json()
        assert any(a["id"] == str(agent.id) for a in agents)


@pytest.mark.django_db
class TestConversationReadStateApi:
    @pytest.fixture
    def conversation_auth_context(self):
        """Create an authenticated org admin with conversation permissions."""
        user_model = get_user_model()
        user = user_model.objects.create_user(
            email=f"conversation-api-{uuid.uuid4().hex[:8]}@example.com",
            password="secret",
        )
        org = Organization.objects.create(
            name=f"Conversation API Org {uuid.uuid4().hex[:6]}",
            slug=f"conversation-api-org-{uuid.uuid4().hex[:10]}",
        )
        Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
        token = generate_api_token()
        APIKey.objects.create(
            user=user,
            name="conversation-api-test-key",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=[APIKeyPermission.CONVERSATIONS_READ.value],
        )
        return {
            "user": user,
            "organization": org,
            "headers": {
                "HTTP_X_API_KEY": token,
                "HTTP_X_ORGANIZATION_ID": str(org.id),
            },
        }

    @pytest.fixture
    def conversation_client(self, conversation_auth_context) -> Client:
        """Django test client authenticated for conversation endpoints."""
        return Client(**conversation_auth_context["headers"])

    def test_mark_conversation_unread_clears_read_timestamp(
        self,
        conversation_client: Client,
        conversation_auth_context,
    ):
        runner = Runner.objects.create(
            name="conversation-runner",
            api_token_hash=hash_token("runner-token-conversation"),
            organization=conversation_auth_context["organization"],
        )
        workspace = Workspace.objects.create(
            runner=runner,
            name="Workspace",
            status=WorkspaceStatus.RUNNING,
            created_by=conversation_auth_context["user"],
        )
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            output="done",
            status=SessionStatus.COMPLETED,
            read_at=timezone.now(),
        )

        response = conversation_client.post(
            "/api/v1/conversations/unread/",
            data={"session_id": str(session.id)},
            content_type="application/json",
        )

        assert response.status_code == 204
        session.refresh_from_db()
        assert session.read_at is None
