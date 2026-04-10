from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, SessionStatus, WorkspaceStatus
from apps.runners.models import Chat, Runner, Session, Workspace
from apps.mcp_app.server import (
    _call_get_workspace,
    _call_list_conversations,
    _call_list_workspaces,
)
from common.utils import hash_token


@pytest.fixture
def mcp_access_setup(db):
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"MCP Access Org {uuid.uuid4().hex[:6]}",
        slug=f"mcp-access-org-{uuid.uuid4().hex[:8]}",
    )
    owner = user_model.objects.create_user(
        email=f"mcp-owner-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    admin = user_model.objects.create_user(
        email=f"mcp-admin-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="mcp-access-runner",
        api_token_hash=hash_token("mcp-access-runner-token"),
        status=RunnerStatus.ONLINE,
        sid="mcp-access-sid",
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
        created_by=admin,
    )
    owner_chat = Chat.objects.create(workspace=owner_workspace, name="Owner Chat")
    admin_chat = Chat.objects.create(workspace=admin_workspace, name="Admin Chat")
    Session.objects.create(chat=owner_chat, prompt="owner", status=SessionStatus.COMPLETED)
    Session.objects.create(chat=admin_chat, prompt="admin", status=SessionStatus.COMPLETED)
    return {
        "org": org,
        "owner_workspace": owner_workspace,
        "admin_workspace": admin_workspace,
        "admin_api_key": SimpleNamespace(user=admin),
    }


def _parse_text_payload(result) -> str:
    assert len(result) == 1
    return result[0].text


@pytest.mark.django_db
def test_mcp_list_workspaces_is_owner_scoped(mcp_access_setup):
    result = _call_list_workspaces(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {},
    )

    payload = json.loads(_parse_text_payload(result))
    assert [entry["id"] for entry in payload] == [str(mcp_access_setup["admin_workspace"].id)]


@pytest.mark.django_db
def test_mcp_get_workspace_rejects_foreign_admin_access(mcp_access_setup):
    result = _call_get_workspace(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {"workspace_id": str(mcp_access_setup["owner_workspace"].id)},
    )

    assert _parse_text_payload(result) == "Error: Workspace not found"


@pytest.mark.django_db
def test_mcp_list_conversations_is_owner_scoped(mcp_access_setup):
    result = _call_list_conversations(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {},
    )

    payload = json.loads(_parse_text_payload(result))
    assert [entry["workspace_id"] for entry in payload] == [
        str(mcp_access_setup["admin_workspace"].id)
    ]
