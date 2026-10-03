from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from django.utils import timezone

from apps.credentials.services import CredentialSvc
from apps.harness.repositories import HarnessSessionRepository
from apps.runners.enums import ProcessStatus
from apps.runners.models import WorkspaceProcess
from apps.runners.services import RunnerService


@pytest.mark.django_db(transaction=True)
def test_credential_resolution_failure_does_not_skip_process_or_desktop_reconcile(
    runner, workspace, monkeypatch
):
    process = WorkspaceProcess.objects.create(
        workspace=workspace,
        command="sleep 60",
        workdir="/workspace",
        name="heartbeat-process",
        pid=1234,
        status=ProcessStatus.RUNNING,
    )
    desktop_sync = Mock()
    monkeypatch.setattr(
        CredentialSvc,
        "resolve_workspace_credentials",
        lambda self, _workspace: (_ for _ in ()).throw(
            ValueError("invalid credential")
        ),
    )
    service = RunnerService()
    service._sync_desktop_state_from_heartbeat = desktop_sync
    monkeypatch.setattr(
        service,
        "auto_stop_inactive_workspaces",
        AsyncMock(return_value=[]),
    )

    service.handle_heartbeat(
        runner,
        [
            {
                "workspace_id": str(workspace.id),
                "status": "running",
                "processes": [
                    {
                        "process_id": str(process.id),
                        "status": "exited",
                        "exit_code": 0,
                        "pid": 1234,
                    }
                ],
                "desktop": {"port": 6901, "container_ip": "10.0.0.1"},
            }
        ],
    )

    process.refresh_from_db()
    assert process.status == ProcessStatus.EXITED
    desktop_sync.assert_called_once_with(
        str(workspace.id),
        {"port": 6901, "container_ip": "10.0.0.1"},
        runner_id=str(runner.id),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_auto_stop_refreshes_busy_harness_state(runner, workspace):
    runner.organization.workspace_auto_stop_timeout_minutes = 1
    runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace.last_activity_at = timezone.now() - timedelta(hours=1)
    workspace.save(update_fields=["last_activity_at"])
    service = RunnerService()

    # Snapshot this row before the scheduled run starts, reproducing the
    # heartbeat's pre-admission annotation. The fresh repository read inside
    # auto-stop must see the busy session and skip the stop.
    stale_workspace = service.workspaces.list_by_runner(runner.id).get(id=workspace.id)
    assert stale_workspace.has_active_harness_session is False
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=runner.organization_id,
    )
    HarnessSessionRepository.mark_status(session, "busy")
    service.stop_workspace = AsyncMock()  # type: ignore[method-assign]

    stopped = await service.auto_stop_inactive_workspaces(
        runner_id=runner.id,
        workspaces=[stale_workspace],
        now=timezone.now(),
    )

    assert stopped == []
    service.stop_workspace.assert_not_awaited()

    # Explicit/manual stops do not use the auto-stop harness-session guard.
    manual_service = RunnerService(sio_server=AsyncMock())
    task = await manual_service.stop_workspace(workspace.id)
    assert task.workspace_id == workspace.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_auto_stop_clears_claim_when_task_creation_fails(
    runner, workspace, monkeypatch
):
    runner.organization.workspace_auto_stop_timeout_minutes = 1
    runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace.last_activity_at = timezone.now() - timedelta(hours=1)
    workspace.save(update_fields=["last_activity_at"])
    service = RunnerService()
    monkeypatch.setattr(
        service.tasks,
        "create",
        Mock(side_effect=RuntimeError("task store unavailable")),
    )

    with pytest.raises(RuntimeError, match="task store unavailable"):
        await service.stop_workspace(workspace.id, auto_stop=True)

    workspace.refresh_from_db()
    assert workspace.active_operation is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_auto_stop_releases_claim_when_dispatch_fails(runner, workspace):
    runner.organization.workspace_auto_stop_timeout_minutes = 1
    runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace.last_activity_at = timezone.now() - timedelta(hours=1)
    workspace.save(update_fields=["last_activity_at"])
    sio = AsyncMock()
    sio.emit.side_effect = RuntimeError("runner unavailable")
    service = RunnerService(sio_server=sio)

    task = await service.stop_workspace(workspace.id, auto_stop=True)
    workspace.refresh_from_db()
    assert workspace.active_operation == "stopping"
    assert workspace.current_task_id == task.id
    assert task.status == "in_progress"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_auto_stop_rechecks_busy_session_when_claiming_stop(runner, workspace):
    from apps.runners.models import Task

    runner.organization.workspace_auto_stop_timeout_minutes = 1
    runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace.last_activity_at = timezone.now() - timedelta(hours=1)
    workspace.save(update_fields=["last_activity_at"])
    service = RunnerService()
    original_stop = service.stop_workspace

    async def admit_run_then_stop(workspace_id, *, auto_stop=False):
        session = HarnessSessionRepository.create(
            workspace_id=workspace.id,
            organization_id=runner.organization_id,
        )
        HarnessSessionRepository.mark_status(session, "busy")
        return await original_stop(workspace_id, auto_stop=auto_stop)

    service.stop_workspace = admit_run_then_stop  # type: ignore[method-assign]
    stopped = await service.auto_stop_inactive_workspaces(
        runner_id=runner.id,
        now=timezone.now(),
    )

    assert stopped == []
    assert not Task.objects.filter(workspace=workspace).exists()
    workspace.refresh_from_db()
    assert workspace.active_operation is None


@pytest.mark.django_db(transaction=True)
def test_current_session_heartbeat_revives_offline_but_old_session_cannot(runner):
    from apps.runners.repositories import RunnerRepository
    from apps.runners.models import Runner

    runner.sid = "current-session"
    runner.status = "offline"
    runner.last_heartbeat_at = timezone.now() - timedelta(seconds=100)
    runner.save()
    stale = Runner.objects.get(pk=runner.pk)
    RunnerRepository.update_heartbeat(runner)
    runner.refresh_from_db()
    assert runner.status == "online"
    assert runner.sid == "current-session"
    assert runner.last_heartbeat_at > timezone.now() - timedelta(seconds=2)
    runner.sid = "replacement-session"
    runner.status = "offline"
    runner.save()
    RunnerRepository.update_heartbeat(stale)
    runner.refresh_from_db()
    assert runner.status == "offline"
    assert runner.sid == "replacement-session"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_offline_current_sid_allowed_only_for_health(runner):
    from asgiref.sync import sync_to_async
    from apps.runners.sio_server import _require_runner_id

    runner.sid = "current-health"
    runner.status = "offline"
    await sync_to_async(runner.save)()
    sio = Mock()
    sio.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    assert await _require_runner_id(sio, "current-health", "runner:heartbeat") == str(
        runner.id
    )
    assert await _require_runner_id(sio, "old-session", "runner:heartbeat") is None
    assert await _require_runner_id(sio, "current-health", "runner:inventory") is None
