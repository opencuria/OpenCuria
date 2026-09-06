"""REST API tests for workspace background processes."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import ProcessStatus, RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace, WorkspaceProcess
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


def _make_context(*, email_prefix: str = "proc-api"):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"{email_prefix}-{uuid.uuid4().hex[:8]}@test.local",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Proc Org {uuid.uuid4().hex[:6]}",
        slug=f"proc-org-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="proc-runner",
        api_token_hash=hash_token(f"proc-token-{uuid.uuid4().hex[:6]}"),
        status=RunnerStatus.ONLINE,
        sid="proc-sid",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Proc Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    return user, org, runner, workspace


def _process_dict(process: WorkspaceProcess) -> dict:
    return {
        "id": process.id,
        "workspace_id": process.workspace_id,
        "name": process.name,
        "command": process.command,
        "workdir": process.workdir,
        "pid": process.pid,
        "log_path": process.log_path,
        "status": process.status,
        "exit_code": process.exit_code,
        "started_at": process.started_at,
        "ended_at": process.ended_at,
        "updated_at": process.updated_at,
    }


@pytest.mark.django_db
def test_processes_require_read_permission(client: Client):
    """Listing without processes_read permission returns 403."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(
        user=user, permissions=[APIKeyPermission.WORKSPACES_READ.value]
    )
    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_processes_require_run_permission_for_start(client: Client):
    """Starting without processes_run permission returns 403."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(
        user=user, permissions=[APIKeyPermission.WORKSPACES_PROCESSES_READ.value]
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_processes_owner_check_returns_404(client: Client):
    """Another user's workspace is invisible (404, not 403)."""
    user, org, runner, workspace = _make_context()
    user_model = get_user_model()
    other = user_model.objects.create_user(
        email=f"other-{uuid.uuid4().hex[:8]}@test.local", password="secret"
    )
    Membership.objects.create(
        user=other, organization=org, role=MembershipRole.MEMBER
    )
    token = _create_api_key(
        user=other,
        permissions=[
            APIKeyPermission.WORKSPACES_PROCESSES_READ.value,
            APIKeyPermission.WORKSPACES_READ.value,
        ],
    )
    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_processes_unknown_process_returns_404(client: Client, monkeypatch):
    """Detail for an unknown process ID returns 404."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(
        user=user,
        permissions=[
            APIKeyPermission.WORKSPACES_PROCESSES_READ.value,
            APIKeyPermission.WORKSPACES_READ.value,
        ],
    )
    from common.exceptions import NotFoundError

    async def _get_process(workspace_id, process_id):
        raise NotFoundError("WorkspaceProcess", str(process_id))

    from apps.runners.services import RunnerService
    from apps.runners.models import Workspace as _Workspace

    real = RunnerService(sio_server=None)

    def _owned(request, org_id, workspace_id):
        workspace = _Workspace.objects.select_related(
            "runner", "created_by"
        ).get(id=workspace_id)
        if workspace.runner.organization_id != org_id:
            from common.exceptions import NotFoundError as _NF

            raise _NF("Workspace", str(workspace_id))
        if workspace.created_by_id != request.user.id:
            from common.exceptions import NotFoundError as _NF

            raise _NF("Workspace", str(workspace_id))
        return workspace

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[]),
            start_process=AsyncMock(),
            get_process=_get_process,
            stop_process=AsyncMock(),
        ),
    )
    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/{uuid.uuid4()}/",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_processes_start_list_stop_happy_path(client: Client, monkeypatch):
    """Start/list/stop happy path returns ProcessOut payloads."""
    from django.utils import timezone

    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(
        user=user,
        permissions=[
            APIKeyPermission.WORKSPACES_PROCESSES_READ.value,
            APIKeyPermission.WORKSPACES_PROCESSES_RUN.value,
            APIKeyPermission.WORKSPACES_READ.value,
        ],
    )

    now = timezone.now()
    process = WorkspaceProcess(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="sleeper",
        command="sleep 60",
        workdir="/workspace",
        pid=4242,
        log_path=".opencuria/processes/abc.log",
        status=ProcessStatus.RUNNING,
        exit_code=None,
        started_at=now,
        ended_at=None,
        updated_at=now,
    )
    stopped = WorkspaceProcess(
        id=process.id,
        workspace_id=workspace.id,
        name="sleeper",
        command="sleep 60",
        workdir="/workspace",
        pid=4242,
        log_path=".opencuria/processes/abc.log",
        status=ProcessStatus.KILLED,
        exit_code=None,
        started_at=now,
        ended_at=now,
        updated_at=now,
    )

    from apps.runners.models import Workspace as _WS2

    def _owned2(request, org_id, workspace_id):
        workspace = _WS2.objects.select_related(
            "runner", "created_by"
        ).get(id=workspace_id)
        if workspace.runner.organization_id != org_id:
            from common.exceptions import NotFoundError as _NF2

            raise _NF2("Workspace", str(workspace_id))
        if workspace.created_by_id != request.user.id:
            from common.exceptions import NotFoundError as _NF2

            raise _NF2("Workspace", str(workspace_id))
        return workspace

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[process]),
            start_process=AsyncMock(return_value=process),
            get_process=AsyncMock(return_value=process),
            stop_process=AsyncMock(return_value=stopped),
        ),
    )

    start = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60", "name": "sleeper"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert start.status_code == 201
    assert start.json()["command"] == "sleep 60"
    assert start.json()["status"] == "running"

    listing = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        **_auth_headers(token, str(org.id)),
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    detail = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/{process.id}/",
        **_auth_headers(token, str(org.id)),
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == str(process.id)

    stop = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/{process.id}/stop/",
        data=json.dumps({"force": False}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert stop.status_code == 200
    assert stop.json()["status"] == "killed"


@pytest.mark.django_db
def test_processes_runner_offline_returns_409(client: Client, monkeypatch):
    """RunnerOfflineError from the service surfaces as 409."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(
        user=user,
        permissions=[
            APIKeyPermission.WORKSPACES_PROCESSES_READ.value,
            APIKeyPermission.WORKSPACES_PROCESSES_RUN.value,
            APIKeyPermission.WORKSPACES_READ.value,
        ],
    )

    from apps.runners.exceptions import RunnerOfflineError

    async def _start(*args, **kwargs):
        raise RunnerOfflineError(str(runner.id))

    from apps.runners.models import Workspace as _WS3

    def _owned3(request, org_id, workspace_id):
        workspace = _WS3.objects.select_related(
            "runner", "created_by"
        ).get(id=workspace_id)
        if workspace.runner.organization_id != org_id:
            from common.exceptions import NotFoundError as _NF3

            raise _NF3("Workspace", str(workspace_id))
        if workspace.created_by_id != request.user.id:
            from common.exceptions import NotFoundError as _NF3

            raise _NF3("Workspace", str(workspace_id))
        return workspace

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[]),
            start_process=_start,
            get_process=AsyncMock(),
            stop_process=AsyncMock(),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 409
