from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import socketio
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners import sio_server
from apps.runners.enums import RunnerStatus, TaskStatus, TaskType, WorkspaceStatus
from apps.runners.models import (
    InventorySnapshot,
    Runner,
    RunnerSystemMetrics,
    Workspace,
)
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
    assert {
        "harness:exec_chunk",
        "harness:exec_done",
        "workspace:cleanup_unknown_done",
        "workspace:cleanup_unknown_failed",
    }.isdisjoint(handlers)
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
    outsider = user_model.objects.create_user(
        email=f"sio-outsider-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    foreign = Workspace.objects.create(
        runner=runner,
        name="Outsider Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=outsider,
    )
    # Owning a workspace without organization membership grants nothing.
    assert (
        await _frontend_user_can_access_workspace(outsider.id, str(foreign.id))
        is False
    )
    assert await _frontend_user_can_access_workspace(owner.id, "not-a-uuid") is False
    assert (
        await _frontend_user_can_access_workspace(owner.id, str(uuid.uuid4()))
        is False
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runner_heartbeat_refreshes_liveness_before_reconciliation(
    runner_sio, runner, monkeypatch
):
    service = sio_server.get_runner_service()
    reconcile = Mock(return_value=[])
    monkeypatch.setattr(service, "handle_heartbeat", reconcile)
    monkeypatch.setattr(service, "reconcile_vanished_processes", AsyncMock())
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - timedelta(hours=1)
    )
    receive = runner_sio.handlers["/"]["runner:heartbeat"]

    await receive(runner.sid, {"workspace_states_observed": False})
    runner.refresh_from_db()
    assert runner.last_heartbeat_at > timezone.now() - timedelta(seconds=5)
    reconcile.assert_not_called()

    await receive(runner.sid, {"workspaces": [{"workspace_id": "w"}]})
    assert reconcile.call_args.kwargs["workspaces"] == [{"workspace_id": "w"}]
    assert reconcile.call_args.kwargs["runner"].id == runner.id
    service.reconcile_vanished_processes.assert_awaited_once()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runner_system_metrics_are_stored_and_old_samples_pruned(
    runner_sio, runner
):
    stale = RunnerSystemMetrics.objects.create(
        runner=runner,
        timestamp=timezone.now() - timedelta(hours=25),
        cpu_usage_percent=1.0,
        ram_used_bytes=1,
        ram_total_bytes=1,
        disk_used_bytes=1,
        disk_total_bytes=1,
    )
    reported_at = (timezone.now() - timedelta(minutes=1)).replace(microsecond=0)
    receive = runner_sio.handlers["/"]["runner:system_metrics"]

    await receive(
        runner.sid,
        {
            "timestamp": reported_at.isoformat().replace("+00:00", "Z"),
            "cpu_usage_percent": 12.5,
            "ram_used_bytes": 2,
            "ram_total_bytes": 4,
            "disk_used_bytes": 6,
            "disk_total_bytes": 8,
            "vm_metrics": {"vm-1": {"cpu": 3}},
        },
    )
    await receive(runner.sid, {"vm_metrics": "not-a-dict"})

    samples = list(RunnerSystemMetrics.objects.filter(runner=runner))
    assert stale.id not in {sample.id for sample in samples}
    reported, defaulted = sorted(samples, key=lambda sample: sample.timestamp)
    assert reported.timestamp == reported_at
    assert (reported.cpu_usage_percent, reported.ram_total_bytes) == (12.5, 4)
    assert reported.vm_metrics == {"vm-1": {"cpu": 3}}
    assert defaulted.timestamp > timezone.now() - timedelta(seconds=5)
    assert (defaulted.cpu_usage_percent, defaulted.disk_total_bytes) == (0.0, 0)
    assert defaulted.vm_metrics is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runner_inventory_is_recorded_for_the_current_session_only(
    runner_sio, runner
):
    def scan(sequence: int, epoch: str) -> dict:
        return {
            "schema_version": 1,
            "inventory_epoch": epoch,
            "inventory_sequence": sequence,
            "complete": True,
            "runtimes": [
                {
                    "runtime_type": runtime,
                    "collected_at": timezone.now().isoformat(),
                    "complete": True,
                    "errors": [],
                    "resources": [],
                }
                for runtime in runner.available_runtimes
            ],
        }

    epoch = str(uuid.uuid4())
    receive = runner_sio.handlers["/"]["runner:inventory"]
    await receive(runner.sid, scan(1, epoch))
    await receive("stale-sid", scan(2, epoch))
    assert list(
        InventorySnapshot.objects.filter(runner=runner).values_list(
            "session", "sequence"
        )
    ) == [(runner.sid, 1)]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_operation_heartbeat_marks_only_the_bound_attempt_executing(
    runner_sio, runner, workspace
):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type=TaskType.STOP_WORKSPACE,
    )
    command = task.lifecyclecommand
    phase = command.phase
    data = OperationRepository.envelope(command, command.payload)
    receive = runner_sio.handlers["/"]["operation:heartbeat"]

    await receive(runner.sid, {**data, "attempt": command.attempt + 1})
    await receive("stale-sid", data)
    command.refresh_from_db()
    assert phase != "executing"
    assert (command.phase, command.heartbeat_at) == (phase, None)

    await receive(runner.sid, data)
    command.refresh_from_db()
    assert command.phase == "executing"
    assert command.heartbeat_at > timezone.now() - timedelta(seconds=5)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runner_register_resolves_the_session_runner(runner_sio, runner):
    handlers = runner_sio.handlers["/"]

    await handlers["runner:register"](runner.sid, {"supported_runtimes": ["qemu"]})
    runner.refresh_from_db()
    assert runner.available_runtimes == ["qemu"]
