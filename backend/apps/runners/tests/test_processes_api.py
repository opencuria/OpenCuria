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
        "run_count": process.run_count,
        "started_at": process.started_at,
        "ended_at": process.ended_at,
        "updated_at": process.updated_at,
    }


def _make_process_record(*, workspace, name="sleeper", run_count=1, status=None):
    """Build an unsaved WorkspaceProcess record for mocked service returns."""
    from django.utils import timezone as _tz

    now = _tz.now()
    return WorkspaceProcess(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=name,
        command="sleep 60",
        workdir="/workspace",
        pid=4242,
        log_path=".opencuria/processes/abc.log",
        status=status or ProcessStatus.RUNNING,
        exit_code=None,
        run_count=run_count,
        started_at=now,
        ended_at=None,
        updated_at=now,
    )


def _full_permissions() -> list[str]:
    return [
        APIKeyPermission.WORKSPACES_PROCESSES_READ.value,
        APIKeyPermission.WORKSPACES_PROCESSES_RUN.value,
        APIKeyPermission.WORKSPACES_READ.value,
    ]


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
        data=json.dumps({"command": "sleep 60", "name": "sleeper"}),
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
            restart_process=AsyncMock(),
            delete_process=AsyncMock(),
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
        run_count=1,
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
        run_count=1,
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
            restart_process=AsyncMock(return_value=process),
            delete_process=AsyncMock(return_value=process.id),
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
            restart_process=AsyncMock(),
            delete_process=AsyncMock(),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60", "name": "sleeper"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 409


@pytest.mark.django_db
def test_processes_start_same_name_upsert_returns_200(client: Client, monkeypatch):
    """Starting with an existing name restarts the same row (200, run_count 2)."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    first = _make_process_record(workspace=workspace, run_count=1)
    second = _make_process_record(workspace=workspace, run_count=2)
    object.__setattr__(second, "id", first.id)

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[second]),
            start_process=AsyncMock(return_value=second),
            get_process=AsyncMock(return_value=second),
            stop_process=AsyncMock(return_value=second),
            restart_process=AsyncMock(return_value=second),
            delete_process=AsyncMock(return_value=second.id),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60", "name": "sleeper"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(first.id)
    assert body["run_count"] == 2


@pytest.mark.django_db
def test_processes_restart_endpoint_returns_200(client: Client, monkeypatch):
    """POST restart returns the restarted row (stable id, bumped run_count)."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    restarted = _make_process_record(workspace=workspace, run_count=3)

    async def _restart(workspace_id, id_or_name, **kwargs):
        assert str(id_or_name) in (str(restarted.id), restarted.name)
        return restarted

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[restarted]),
            start_process=AsyncMock(return_value=restarted),
            get_process=AsyncMock(return_value=restarted),
            stop_process=AsyncMock(return_value=restarted),
            restart_process=_restart,
            delete_process=AsyncMock(return_value=restarted.id),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/{restarted.id}/restart/",
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 200
    assert response.json()["id"] == str(restarted.id)
    assert response.json()["run_count"] == 3


@pytest.mark.django_db
def test_processes_restart_unknown_returns_404(client: Client, monkeypatch):
    """Restart of an unknown process returns 404."""
    import apps.runners.api as runners_api
    from common.exceptions import NotFoundError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    async def _restart(workspace_id, id_or_name, **kwargs):
        raise NotFoundError("WorkspaceProcess", str(id_or_name))

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[]),
            start_process=AsyncMock(),
            get_process=AsyncMock(),
            stop_process=AsyncMock(),
            restart_process=_restart,
            delete_process=AsyncMock(),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/{uuid.uuid4()}/restart/",
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_processes_delete_returns_204_then_get_404(client: Client, monkeypatch):
    """DELETE removes the row (204); a later GET is 404."""
    import apps.runners.api as runners_api
    from common.exceptions import NotFoundError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    record = _make_process_record(workspace=workspace, run_count=1)

    async def _delete(workspace_id, id_or_name):
        return record.id

    async def _missing(workspace_id, id_or_name):
        raise NotFoundError("WorkspaceProcess", str(id_or_name))

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    service = SimpleNamespace(
        list_processes=AsyncMock(return_value=[record]),
        start_process=AsyncMock(return_value=record),
        get_process=AsyncMock(return_value=record),
        stop_process=AsyncMock(return_value=record),
        restart_process=AsyncMock(return_value=record),
        delete_process=_delete,
    )
    monkeypatch.setattr(runners_api, "_get_service", lambda: service)

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace.id}/processes/{record.id}/",
        **_auth_headers(token, str(org.id)),
    )
    assert deleted.status_code == 204

    service.get_process = _missing
    detail = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/{record.id}/",
        **_auth_headers(token, str(org.id)),
    )
    assert detail.status_code == 404


@pytest.mark.django_db
def test_processes_delete_unknown_returns_404(client: Client, monkeypatch):
    """DELETE of an unknown process returns 404."""
    import apps.runners.api as runners_api
    from common.exceptions import NotFoundError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    async def _delete(workspace_id, id_or_name):
        raise NotFoundError("WorkspaceProcess", str(id_or_name))

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[]),
            start_process=AsyncMock(),
            get_process=AsyncMock(),
            stop_process=AsyncMock(),
            restart_process=AsyncMock(),
            delete_process=_delete,
        ),
    )
    response = client.delete(
        f"/api/v1/workspaces/{workspace.id}/processes/{uuid.uuid4()}/",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_processes_get_and_stop_by_name(client: Client, monkeypatch):
    """Detail/stop resolve an exact process name, not just the UUID."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    record = _make_process_record(workspace=workspace, name="sleeper", run_count=2)
    seen: list[tuple] = []

    async def _get(workspace_id, id_or_name):
        seen.append(("get", str(id_or_name)))
        assert str(id_or_name) == "sleeper"
        return record

    async def _stop(workspace_id, id_or_name):
        seen.append(("stop", str(id_or_name)))
        assert str(id_or_name) == "sleeper"
        return record

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[record]),
            start_process=AsyncMock(return_value=record),
            get_process=_get,
            stop_process=_stop,
            restart_process=AsyncMock(return_value=record),
            delete_process=AsyncMock(return_value=record.id),
        ),
    )
    detail = client.get(
        f"/api/v1/workspaces/{workspace.id}/processes/sleeper/",
        **_auth_headers(token, str(org.id)),
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == str(record.id)

    stopped = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/sleeper/stop/",
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert stopped.status_code == 200
    assert [kind for kind, _ in seen] == ["get", "stop"]


@pytest.mark.django_db
def test_processes_start_without_name_returns_400(client: Client, monkeypatch):
    """Start without a name is a 400 (ValueError), not a 422 schema error."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    async def _start(workspace_id, command, **kwargs):
        raise ValueError("name must not be empty")

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[]),
            start_process=_start,
            get_process=AsyncMock(),
            stop_process=AsyncMock(),
            restart_process=AsyncMock(),
            delete_process=AsyncMock(),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60", "name": ""}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_processes_start_missing_name_field_is_422(client: Client, monkeypatch):
    """A missing name field fails schema validation (422) — name is required."""
    import apps.runners.api as runners_api

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_full_permissions())

    monkeypatch.setattr(runners_api, "_get_owned_workspace_async", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runners_api,
        "_get_service",
        lambda: SimpleNamespace(
            list_processes=AsyncMock(return_value=[]),
            start_process=AsyncMock(),
            get_process=AsyncMock(),
            stop_process=AsyncMock(),
            restart_process=AsyncMock(),
            delete_process=AsyncMock(),
        ),
    )
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/processes/",
        data=json.dumps({"command": "sleep 60"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 422
