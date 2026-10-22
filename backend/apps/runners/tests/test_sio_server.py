from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import socketio
from django.contrib.auth import get_user_model

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners import sio_server
from apps.runners.enums import RunnerStatus, TaskStatus, TaskType, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.runners.operations import RESULT_METHODS, OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services import RunnerService
from apps.runners.sio_server import _frontend_user_can_access_workspace
from common.utils import hash_token


def test_runner_reply_registration() -> None:
    """Lifecycle results require an envelope; active RPC and stream replies remain."""
    sio = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(sio)
    handlers = sio.handlers["/"]
    assert (RESULT_METHODS.keys() - {"workspace:error"}).isdisjoint(handlers)
    assert {"harness:exec_chunk", "harness:exec_done"}.isdisjoint(handlers)
    assert {
        "operation:result",
        "workspace:error",
        "workspace:credentials_injected",
        "image:build_progress",
        "harness:exec_wait_result",
        "workspace:stream_output",
    } <= handlers.keys()


@pytest.fixture
def runner_sio(monkeypatch, runner):
    """Exercise registered handlers with real services and committed task records."""
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    server.emit = AsyncMock()
    server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    service = RunnerService(sio_server=server, frontend_bus=lambda *args: None)
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    return server


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        TaskType.START_TERMINAL,
        TaskType.START_DESKTOP,
        TaskType.STOP_DESKTOP,
        TaskType.INJECT_CREDENTIALS,
    ],
)
async def test_direct_rpc_errors_remain_authenticated(runner_sio, runner, workspace, kind):
    task = TaskRepository.create(
        task_id=uuid.uuid4(), runner=runner, workspace=workspace, task_type=kind
    )
    receive = runner_sio.handlers["/"]["workspace:error"]
    data = {"task_id": str(task.id), "error": "RPC failed"}
    runner_sio.get_session.return_value = {}
    await receive(runner.sid, data)
    runner_sio.get_session.return_value = {"runner_id": str(runner.id)}
    await receive("stale-sid", data)
    task.refresh_from_db()
    assert task.status == TaskStatus.PENDING

    await receive(runner.sid, data)
    task.refresh_from_db()
    assert task.status == TaskStatus.FAILED
    assert task.error == data["error"]
    runner_sio.emit.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_lifecycle_completion_requires_bound_result(runner_sio, runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=TaskType.STOP_WORKSPACE,
    )
    command = task.lifecyclecommand
    data = OperationRepository.envelope(
        command, {**command.payload, "credentials_present": False}
    )
    await runner_sio.handlers["/"]["workspace:error"](
        runner.sid, {"task_id": str(task.id), "error": "Unbound lifecycle error"}
    )
    receive = runner_sio.handlers["/"]["operation:result"]
    for sid, result in [
        ("stale-sid", data),
        (runner.sid, {**data, "attempt": command.attempt + 1}),
    ]:
        await receive(sid, {"event": "workspace:stopped", "data": result})
    task.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == TaskStatus.PENDING
    assert workspace.status == WorkspaceStatus.RUNNING
    assert workspace.current_task_id == task.id
    runner_sio.emit.assert_not_awaited()

    packet = {"event": "workspace:stopped", "data": data}
    await receive(runner.sid, packet)
    await receive(runner.sid, packet)  # Journal replay stays ACKable.
    task.refresh_from_db()
    workspace.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    assert workspace.status == WorkspaceStatus.STOPPED
    assert workspace.credentials_present is False
    assert workspace.current_task_id is None
    assert runner_sio.emit.await_count == 2
    runner_sio.emit.assert_awaited_with(
        "operation:ack",
        {key: data[key] for key in ("operation_id", "attempt", "target", "runner_id")},
        to=runner.sid,
    )


# Async ORM calls use separate connections and need committed fixture data.
@pytest.mark.django_db(transaction=True)
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

    assert (
        await _frontend_user_can_access_workspace(owner.id, str(workspace.id)) is True
    )
    assert (
        await _frontend_user_can_access_workspace(admin.id, str(workspace.id)) is False
    )
