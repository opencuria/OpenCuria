from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.runners import api as runners_api
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, SessionStatus, WorkspaceStatus
from apps.runners.models import Chat, ImageInstance, Runner, Session, Workspace
from common.utils import generate_api_token, hash_token


def _make_client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"ws-access-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


@pytest.fixture
def workspace_access_setup(db):
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Workspace Access Org {uuid.uuid4().hex[:6]}",
        slug=f"workspace-access-org-{uuid.uuid4().hex[:8]}",
    )
    owner = user_model.objects.create_user(
        email=f"owner-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    foreign_admin = user_model.objects.create_user(
        email=f"foreign-admin-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    foreign_member = user_model.objects.create_user(
        email=f"foreign-member-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=foreign_admin,
        organization=org,
        role=MembershipRole.ADMIN,
    )
    Membership.objects.create(
        user=foreign_member,
        organization=org,
        role=MembershipRole.MEMBER,
    )

    runner = Runner.objects.create(
        name="workspace-access-runner",
        api_token_hash=hash_token("workspace-access-runner-token"),
        status=RunnerStatus.ONLINE,
        sid="workspace-access-runner-sid",
        organization=org,
        available_runtimes=["docker"],
    )
    owner_workspace = Workspace.objects.create(
        runner=runner,
        name="Owner Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    admin_workspace = Workspace.objects.create(
        runner=runner,
        name="Admin Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=foreign_admin,
    )
    owner_chat = Chat.objects.create(workspace=owner_workspace, name="Owner Chat")
    admin_chat = Chat.objects.create(workspace=admin_workspace, name="Admin Chat")
    owner_session = Session.objects.create(
        chat=owner_chat,
        prompt="owner prompt",
        status=SessionStatus.RUNNING,
    )
    Session.objects.create(
        chat=admin_chat,
        prompt="admin prompt",
        status=SessionStatus.COMPLETED,
    )
    artifact = ImageInstance.objects.create(
        runner=runner,
        origin_workspace=owner_workspace,
        created_by=owner,
        name="Owner Snapshot",
        runner_ref="owner-snapshot-1",
        status=ImageInstance.Status.READY,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
    )
    return {
        "org": org,
        "owner": owner,
        "foreign_admin": foreign_admin,
        "foreign_member": foreign_member,
        "owner_workspace": owner_workspace,
        "admin_workspace": admin_workspace,
        "owner_chat": owner_chat,
        "owner_session": owner_session,
        "artifact": artifact,
    }


@pytest.mark.django_db
def test_admin_only_sees_own_workspaces(workspace_access_setup):
    client = _make_client(
        user=workspace_access_setup["foreign_admin"],
        org=workspace_access_setup["org"],
        permissions=[APIKeyPermission.WORKSPACES_READ.value],
    )

    response = client.get("/api/v1/workspaces/")

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()] == [
        str(workspace_access_setup["admin_workspace"].id)
    ]


@pytest.mark.django_db
def test_admin_only_sees_own_conversations(workspace_access_setup):
    client = _make_client(
        user=workspace_access_setup["foreign_admin"],
        org=workspace_access_setup["org"],
        permissions=[APIKeyPermission.CONVERSATIONS_READ.value],
    )

    response = client.get("/api/v1/conversations/")

    assert response.status_code == 200
    assert [entry["workspace_id"] for entry in response.json()] == [
        str(workspace_access_setup["admin_workspace"].id)
    ]


@pytest.mark.django_db
def test_global_image_artifact_create_requires_workspace_owner(workspace_access_setup):
    client = _make_client(
        user=workspace_access_setup["foreign_admin"],
        org=workspace_access_setup["org"],
        permissions=[APIKeyPermission.IMAGES_CREATE.value],
    )

    response = client.post(
        "/api/v1/image-artifacts/",
        data=json.dumps(
            {
                "workspace_id": str(workspace_access_setup["owner_workspace"].id),
                "name": "forbidden-snapshot",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_global_image_artifact_create_requires_images_create_permission(
    workspace_access_setup,
    monkeypatch,
):
    client = _make_client(
        user=workspace_access_setup["owner"],
        org=workspace_access_setup["org"],
        permissions=[],
    )

    def _unexpected_service():
        raise AssertionError("service should not be resolved without images:create")

    monkeypatch.setattr(runners_api, "_get_service", _unexpected_service)

    response = client.post(
        "/api/v1/image-artifacts/",
        data=json.dumps(
            {
                "workspace_id": str(workspace_access_setup["owner_workspace"].id),
                "name": "blocked-snapshot",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
@pytest.mark.parametrize("actor_key", ["foreign_admin", "foreign_member"])
@pytest.mark.parametrize(
    ("method", "path_template", "payload", "permissions"),
    [
        ("get", "/api/v1/workspaces/{workspace_id}/", None, [APIKeyPermission.WORKSPACES_READ.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/prompt/", {"prompt": "test"}, [APIKeyPermission.PROMPTS_RUN.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel/", None, [APIKeyPermission.PROMPTS_CANCEL.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/terminal/", {"cols": 80, "rows": 24}, [APIKeyPermission.TERMINAL_ACCESS.value]),
        ("patch", "/api/v1/workspaces/{workspace_id}/", {"name": "renamed"}, [APIKeyPermission.WORKSPACES_UPDATE.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/stop/", None, [APIKeyPermission.WORKSPACES_STOP.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/resume/", None, [APIKeyPermission.WORKSPACES_RESUME.value]),
        ("delete", "/api/v1/workspaces/{workspace_id}/", None, [APIKeyPermission.WORKSPACES_DELETE.value]),
        ("get", "/api/v1/workspaces/{workspace_id}/sessions/", None, [APIKeyPermission.CONVERSATIONS_READ.value]),
        ("get", "/api/v1/workspaces/{workspace_id}/chats/", None, [APIKeyPermission.CONVERSATIONS_READ.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/chats/", {"name": "new chat"}, [APIKeyPermission.PROMPTS_RUN.value]),
        ("patch", "/api/v1/workspaces/{workspace_id}/chats/{chat_id}/", {"name": "new name"}, [APIKeyPermission.PROMPTS_RUN.value]),
        ("delete", "/api/v1/workspaces/{workspace_id}/chats/{chat_id}/", None, [APIKeyPermission.PROMPTS_RUN.value]),
        ("get", "/api/v1/workspaces/{workspace_id}/chats/{chat_id}/sessions/", None, [APIKeyPermission.CONVERSATIONS_READ.value]),
        ("get", "/api/v1/workspaces/{workspace_id}/image-artifacts/", None, [APIKeyPermission.IMAGES_READ.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/image-artifacts/", {"name": "snapshot"}, [APIKeyPermission.IMAGES_CREATE.value]),
        ("delete", "/api/v1/workspaces/{workspace_id}/image-artifacts/{artifact_id}/", None, [APIKeyPermission.IMAGES_DELETE.value]),
        ("post", "/api/v1/workspaces/{workspace_id}/image-artifacts/{artifact_id}/workspaces/", {"name": "clone"}, [APIKeyPermission.IMAGES_CLONE.value]),
    ],
)
def test_foreign_workspace_endpoints_return_not_found(
    workspace_access_setup,
    actor_key: str,
    method: str,
    path_template: str,
    payload: dict | None,
    permissions: list[str],
):
    client = _make_client(
        user=workspace_access_setup[actor_key],
        org=workspace_access_setup["org"],
        permissions=permissions,
    )
    path = path_template.format(
        workspace_id=workspace_access_setup["owner_workspace"].id,
        session_id=workspace_access_setup["owner_session"].id,
        chat_id=workspace_access_setup["owner_chat"].id,
        artifact_id=workspace_access_setup["artifact"].id,
    )

    request = getattr(client, method)
    if payload is None:
        response = request(path)
    else:
        response = request(
            path,
            data=json.dumps(payload),
            content_type="application/json",
        )

    assert response.status_code == 404
