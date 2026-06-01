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
from apps.runners import api as runners_api
from apps.runners.enums import SessionStatus, WorkspaceStatus
from apps.runners.models import (
    AgentDefinition,
    Chat,
    OrgAgentDefinitionActivation,
    Runner,
    Session,
    Workspace,
    WorkspaceDesktopStartCommand,
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
            APIKeyPermission.WORKSPACES_UPDATE.value,
            APIKeyPermission.TERMINAL_ACCESS.value,
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
class TestWorkspaceDesktopStartCommandApi:
    def test_lists_workspace_desktop_start_commands(self, client, auth_context):
        runner = Runner.objects.create(
            name="desktop-command-runner",
            api_token_hash=hash_token("runner-token-desktop-list"),
            organization=auth_context["organization"],
        )
        workspace = Workspace.objects.create(
            runner=runner,
            name="Workspace With Desktop Commands",
            status=WorkspaceStatus.RUNNING,
            created_by=auth_context["user"],
        )
        command = WorkspaceDesktopStartCommand.objects.create(
            workspace=workspace,
            name="Docs",
            command="xdg-open https://docs.example.test",
        )

        response = client.get(
            f"/api/v1/workspaces/{workspace.id}/desktop-start-commands/"
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(command.id)
        assert data[0]["workspace_id"] == str(workspace.id)
        assert data[0]["name"] == "Docs"
        assert data[0]["command"] == "xdg-open https://docs.example.test"

    def test_creates_updates_and_deletes_workspace_desktop_start_commands(
        self,
        client,
        auth_context,
    ):
        runner = Runner.objects.create(
            name="desktop-command-runner",
            api_token_hash=hash_token("runner-token-desktop-write"),
            organization=auth_context["organization"],
        )
        workspace = Workspace.objects.create(
            runner=runner,
            name="Workspace With Desktop Commands",
            status=WorkspaceStatus.RUNNING,
            created_by=auth_context["user"],
        )

        create_response = client.post(
            f"/api/v1/workspaces/{workspace.id}/desktop-start-commands/",
            data={"name": "Docs", "command": "xdg-open https://docs.example.test"},
            content_type="application/json",
        )
        assert create_response.status_code == 201
        created_id = create_response.json()["id"]

        update_response = client.patch(
            f"/api/v1/workspaces/{workspace.id}/desktop-start-commands/{created_id}/",
            data={"name": "Documentation"},
            content_type="application/json",
        )
        assert update_response.status_code == 200
        assert update_response.json()["name"] == "Documentation"

        delete_response = client.delete(
            f"/api/v1/workspaces/{workspace.id}/desktop-start-commands/{created_id}/"
        )
        assert delete_response.status_code == 204
        assert (
            WorkspaceDesktopStartCommand.objects.filter(id=created_id).exists() is False
        )

    def test_start_desktop_forwards_selected_command_id(self, client, auth_context, monkeypatch):
        runner = Runner.objects.create(
            name="desktop-command-runner",
            api_token_hash=hash_token("runner-token-desktop-start"),
            organization=auth_context["organization"],
        )
        workspace = Workspace.objects.create(
            runner=runner,
            name="Workspace With Desktop Commands",
            status=WorkspaceStatus.RUNNING,
            created_by=auth_context["user"],
        )
        command = WorkspaceDesktopStartCommand.objects.create(
            workspace=workspace,
            name="Docs",
            command="xdg-open https://docs.example.test",
        )

        class DummyService:
            def get_workspace_for_user(self, workspace_id, *, user, organization_id):
                assert workspace_id == workspace.id
                assert user == auth_context["user"]
                assert organization_id == auth_context["organization"].id
                return workspace

            async def start_desktop(self, workspace_id, desktop_start_command_id=None):
                assert workspace_id == workspace.id
                assert desktop_start_command_id == command.id
                return type("Task", (), {"id": uuid.uuid4()})()

        monkeypatch.setattr(runners_api, "_get_service", lambda: DummyService())

        response = client.post(
            f"/api/v1/workspaces/{workspace.id}/desktop/",
            data={"desktop_start_command_id": str(command.id)},
            content_type="application/json",
        )

        assert response.status_code == 202
        assert "task_id" in response.json()


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
