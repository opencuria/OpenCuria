from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.runners.sio_server import _frontend_user_can_access_workspace
from common.utils import hash_token


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_frontend_workspace_access_is_owner_scoped():
    user_model = get_user_model()
    owner = user_model.objects.create_user(
        email=f"sio-owner-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    admin = user_model.objects.create_user(
        email=f"sio-admin-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"SIO Access Org {uuid.uuid4().hex[:6]}",
        slug=f"sio-access-org-{uuid.uuid4().hex[:8]}",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="sio-runner",
        api_token_hash=hash_token("sio-runner-token"),
        status=RunnerStatus.ONLINE,
        sid="sio-sid",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Owner Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )

    assert await _frontend_user_can_access_workspace(owner.id, str(workspace.id)) is True
    assert await _frontend_user_can_access_workspace(admin.id, str(workspace.id)) is False
