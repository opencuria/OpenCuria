"""REST contracts for the background-process controls used by the webapp."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole
from apps.runners import api as runners_api
from apps.runners.enums import ProcessStatus
from apps.runners.models import WorkspaceProcess
from apps.runners.services import RunnerService
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db

# Method and suffix of each process control used by the webapp.
CONTROLS = [
    ("get", ""),
    ("get", "?live=true"),
    ("post", "{process_id}/stop/"),
    ("post", "{process_id}/restart/"),
    ("delete", "{process_id}/"),
]


@pytest.fixture
def api_key(user, organization) -> tuple[APIKey, str]:
    """Authorize process controls through a real organization membership and key."""
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    token = generate_api_token()
    key = APIKey.objects.create(
        user=user,
        name="process-controls",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[
            APIKeyPermission.WORKSPACES_PROCESSES_READ.value,
            APIKeyPermission.WORKSPACES_PROCESSES_RUN.value,
        ],
    )
    return key, token


@pytest.fixture
def client(api_key, organization) -> Client:
    """Use API-key authentication and the same org header as the webapp."""
    return Client(
        HTTP_X_API_KEY=api_key[1], HTTP_X_ORGANIZATION_ID=str(organization.id)
    )


@pytest.fixture
def service(monkeypatch) -> RunnerService:
    """Exercise the real service and repositories, replacing only runner I/O."""
    service = RunnerService()
    service._await_process_result = AsyncMock(return_value={"pid": 4242})
    monkeypatch.setattr(runners_api, "_get_service", lambda: service)
    return service


@pytest.fixture
def process(workspace) -> WorkspaceProcess:
    """A never-started definition, as restored from a captured image."""
    return WorkspaceProcess.objects.create(
        workspace=workspace,
        name="configured",
        command="python -m app",
        workdir="/app",
        status=ProcessStatus.EXITED,
        run_count=0,
    )


def _url(workspace, suffix: str = "", process_id: object = "") -> str:
    return f"/api/v1/workspaces/{workspace.id}/processes/" + suffix.format(
        process_id=process_id
    )


def test_list_start_restart_stop_delete(client, service, workspace, process):
    """Both UI start and restart use the stored definition through /restart/."""
    listing = client.get(_url(workspace))
    assert listing.status_code == 200
    row = listing.json()[0]
    assert row["id"] == str(process.id)
    assert row["run_count"] == 0
    assert row["status"] == ProcessStatus.EXITED
    assert row["pid"] is None and row["log_path"] == ""
    service._await_process_result.assert_not_awaited()

    live_listing = client.get(_url(workspace, "?live=true"))
    assert live_listing.status_code == 200
    assert service._await_process_result.await_args.kwargs["event"] == (
        "harness:process_list"
    )
    service._await_process_result.reset_mock()

    for run_count in (1, 2):
        started = client.post(_url(workspace, "{process_id}/restart/", process.id))
        assert started.status_code == 200
        body = started.json()
        assert body["id"] == str(process.id)
        assert body["run_count"] == run_count
        assert body["status"] == ProcessStatus.RUNNING
        payload = service._await_process_result.await_args.kwargs["payload"]
        assert payload["process_id"] == str(process.id)
        assert payload["command"] == process.command
        assert payload["workdir"] == process.workdir
        assert payload["env"] == {}
        suffix = "" if run_count == 1 else "_r2"
        assert payload["log_path"] == (
            f"/workspace/.opencuria/processes/{process.id}{suffix}.log"
        )
    events = [
        call.kwargs["event"] for call in service._await_process_result.await_args_list
    ]
    assert events == [
        "harness:process_start",
        "harness:process_get",
        "harness:process_stop",
        "harness:process_start",
    ]

    stopped = client.post(_url(workspace, "{process_id}/stop/", process.id))
    assert stopped.status_code == 200
    assert stopped.json()["status"] == ProcessStatus.KILLED
    deleted = client.delete(_url(workspace, "{process_id}/", process.id))
    assert deleted.status_code == 204
    assert client.get(_url(workspace)).json() == []
    assert not WorkspaceProcess.objects.filter(id=process.id).exists()


@pytest.mark.parametrize("live", [False, True])
def test_list_hides_finished_temporary_processes(client, service, workspace, live):
    """Finished temps stay in agent history while running temps remain visible."""
    running_id = uuid.uuid4()
    service._await_process_result.return_value = {
        "processes": [{"process_id": str(running_id), "pid": 4242}]
    }
    for status, process_id in (
        (ProcessStatus.RUNNING, running_id),
        (ProcessStatus.EXITED, uuid.uuid4()),
    ):
        WorkspaceProcess.objects.create(
            id=process_id,
            workspace=workspace,
            name=str(process_id),
            command="sleep 60",
            kind="temp",
            session_id=uuid.uuid4(),
            status=status,
            pid=4242,
        )
    response = client.get(_url(workspace, "?live=true" if live else ""))
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [str(running_id)]
    assert WorkspaceProcess.objects.filter(workspace=workspace).count() == 2


@pytest.mark.parametrize("method,suffix", CONTROLS)
def test_controls_require_permission(client, api_key, workspace, method, suffix):
    """Read/run permissions protect each control before service dispatch."""
    key, _ = api_key
    key.permissions = []
    key.save(update_fields=["permissions"])
    response = getattr(client, method)(_url(workspace, suffix, uuid.uuid4()))
    assert response.status_code == 403


@pytest.mark.parametrize("method,suffix", CONTROLS)
def test_controls_hide_another_owners_workspace(
    client, service, workspace, process, method, suffix
):
    """Organization membership alone does not grant access to another workspace."""
    workspace.created_by = get_user_model().objects.create_user(
        email="other-process-owner@test.local", password="secret"
    )
    workspace.save(update_fields=["created_by"])
    response = getattr(client, method)(_url(workspace, suffix, process.id))
    assert response.status_code == 404
    service._await_process_result.assert_not_awaited()
    process.refresh_from_db()
    assert process.run_count == 0


@pytest.mark.parametrize("method,suffix", CONTROLS[2:])
def test_mutations_reject_unknown_process(client, service, workspace, method, suffix):
    """A missing process returns 404 without contacting the runner."""
    response = getattr(client, method)(_url(workspace, suffix, uuid.uuid4()))
    assert response.status_code == 404
    service._await_process_result.assert_not_awaited()


def test_restart_reports_runner_failure(client, service, workspace, process):
    """A failed start is reported to the UI and persisted on the existing row."""
    service._await_process_result.return_value = {"error": "spawn failed"}
    response = client.post(_url(workspace, "{process_id}/restart/", process.id))
    assert response.status_code == 409
    process.refresh_from_db()
    assert process.status == ProcessStatus.FAILED


@pytest.mark.parametrize("method,suffix", [("post", ""), ("get", "{process_id}/")])
def test_unused_creation_and_detail_methods_are_not_exposed(
    client, workspace, method, suffix
):
    """The process REST API exposes only controls consumed by the webapp."""
    response = getattr(client, method)(_url(workspace, suffix, uuid.uuid4()))
    assert response.status_code == 405
