"""Current runner desktop events, heartbeat recovery and stale-event fencing."""

import uuid
from unittest.mock import AsyncMock

import pytest
import socketio

from apps.runners import sio_server
from apps.runners.enums import TaskStatus, TaskType, WorkspaceStatus
from apps.runners.models import Task
from apps.runners.services import RunnerService

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def state():
    return {
        "port": 6901,
        "guest_ip": "10.100.0.2",
        "viewer": True,
        "computer_use": True,
        "mcp": True,
        "holder_count": 3,
        "generation": 5,
        "epoch": "desktop-epoch",
    }


@pytest.fixture
def service(runner, monkeypatch):
    service = RunnerService(sio_server=AsyncMock())
    service._set_runner_desktop_epoch(str(runner.id), "desktop-epoch")
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    return service


@pytest.fixture
def emit(monkeypatch):
    emit = AsyncMock()
    monkeypatch.setattr("apps.runners.services.infra.realtime.emit_to_frontend", emit)
    return emit


@pytest.fixture
def send(service, runner, workspace, state, monkeypatch):
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)

    async def send(event, *, sender=None, **payload):
        monkeypatch.setattr(
            sio_server,
            "_require_runner_id",
            AsyncMock(return_value=sender or str(runner.id)),
        )
        await server.handlers["/"][f"desktop:{event}"](
            "sid",
            {
                **state,
                "workspace_id": str(workspace.id),
                "task_id": str(uuid.uuid4()),
                **payload,
            },
        )

    return send


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["started", "process"])
async def test_active_events_publish_complete_state(
    service, runner, workspace, state, send, emit, event
):
    task = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type=TaskType.START_DESKTOP,
        status=TaskStatus.IN_PROGRESS,
    )
    for _ in range(2):
        await send(event, task_id=str(task.id))
        assert service.get_desktop_info(str(workspace.id)) == state
        summary = emit.await_args.args[1]
        assert summary["proxy_url"] == f"/ws/desktop/{workspace.id}/"
        assert summary["computer_use_active"] is True
        assert summary["mcp_active"] is True
        assert summary["viewer_held"] is True
        assert summary["holder_count"] == 3
        assert summary["generation"] == 5
        assert summary["epoch"] == state["epoch"]
    assert emit.await_count == 2
    task.refresh_from_db()
    assert task.status == (
        TaskStatus.COMPLETED if event == "started" else TaskStatus.IN_PROGRESS
    )


@pytest.mark.asyncio
async def test_viewer_release_keeps_remaining_holders(
    service, runner, workspace, send, emit
):
    await send("process")
    task = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type=TaskType.STOP_DESKTOP,
        status=TaskStatus.IN_PROGRESS,
    )
    await send("viewer_released", task_id=str(task.id), holder_count=2)
    assert service.get_desktop_info(str(workspace.id))["holder_count"] == 2
    assert emit.await_args.args[1]["viewer_held"] is True
    await send(
        "viewer_released",
        task_id=str(task.id),
        viewer=False,
        computer_use=False,
        mcp=True,
        holder_count=1,
    )
    assert service.get_desktop_info(str(workspace.id))["viewer"] is False
    assert emit.await_args.args[1]["computer_use_active"] is False
    assert emit.await_args.args[1]["mcp_active"] is True
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("with_task", [False, True])
async def test_stop_clears_route_and_fences_late_active_events(
    service, runner, workspace, state, send, emit, with_task
):
    await send("process")
    task = (
        Task.objects.create(
            runner=runner,
            workspace=workspace,
            type=TaskType.STOP_DESKTOP,
            status=TaskStatus.IN_PROGRESS,
        )
        if with_task
        else None
    )
    await send("stopped", task_id=str(task.id) if task else None)
    assert service.get_desktop_info(str(workspace.id)) is None
    assert service._desktop_state_versions[str(workspace.id)] == (
        str(runner.id),
        state["epoch"],
        5,
        False,
    )
    assert emit.await_args.args[0] == "desktop:stopped"
    if task:
        task.refresh_from_db()
        assert task.status == TaskStatus.COMPLETED
    emit.reset_mock()
    await send("process")
    assert service.get_desktop_info(str(workspace.id)) is None
    emit.assert_not_awaited()
    await send("process", generation=6)
    assert service.get_desktop_info(str(workspace.id))["generation"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["started", "process", "stopped", "viewer_released"])
@pytest.mark.parametrize(
    "metadata",
    [{"generation": 4}, {"epoch": "old-epoch"}, {"generation": None}, {"epoch": None}],
)
async def test_stale_or_unversioned_events_cannot_change_state(
    service, workspace, state, send, emit, event, metadata
):
    await send("process")
    emit.reset_mock()
    await send(event, guest_ip="wrong-ip", **metadata)
    assert service.get_desktop_info(str(workspace.id)) == state
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_epoch_replaces_previous_runner_process(
    service, workspace, send
):
    await send("process")
    runner_id = service._desktop_workspace_runner[str(workspace.id)]
    service._set_runner_desktop_epoch(runner_id, "new-epoch")
    await send("process", epoch="new-epoch", generation=1)
    await send("stopped")
    assert service.get_desktop_info(str(workspace.id))["epoch"] == "new-epoch"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["stopped", "viewer_released"])
async def test_delayed_task_completion_cannot_overwrite_a_newer_desktop(
    service, runner, workspace, state, send, emit, monkeypatch, event
):
    await send("process")
    task = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type=TaskType.STOP_DESKTOP,
        status=TaskStatus.IN_PROGRESS,
    )
    newer = {**state, "generation": 6}

    def complete(_task):
        service._record_active_desktop(
            str(workspace.id), newer, runner_id=str(runner.id)
        )

    monkeypatch.setattr(service.tasks, "complete", complete)
    emit.reset_mock()
    await send(event, task_id=str(task.id), viewer=False)
    assert service.get_desktop_info(str(workspace.id)) == newer
    emit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["process", "stopped", "viewer_released"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("missing_workspace", [False, True])
async def test_events_require_real_workspace_ownership(
    service, workspace, state, send, emit, event, cached, missing_workspace
):
    if cached:
        await send("process")
        # Force ownership validation against the database with an empty owner cache.
        service._desktop_workspace_runner.pop(str(workspace.id))
    emit.reset_mock()
    foreign_runner = str(uuid.uuid4())
    service._set_runner_desktop_epoch(foreign_runner, state["epoch"])
    await send(
        event,
        sender=foreign_runner,
        workspace_id=str(uuid.uuid4()) if missing_workspace else str(workspace.id),
    )
    assert service.get_desktop_info(str(workspace.id)) == (state if cached else None)
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_lookup_is_async_safe(service, runner, workspace, monkeypatch):
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    assert await service._desktop_event_owned_by_runner(
        str(workspace.id), str(runner.id)
    )
    assert not await service._desktop_event_owned_by_runner(
        str(uuid.uuid4()), str(runner.id)
    )


@pytest.mark.parametrize(
    "desktop,tracked,generation,epoch,active",
    [
        (True, True, 5, "desktop-epoch", True),
        (False, True, 5, "desktop-epoch", True),
        (False, False, 5, "desktop-epoch", False),
        (False, False, 4, "desktop-epoch", True),
        (True, True, 4, "desktop-epoch", True),
        (False, False, 5, "old-epoch", True),
    ],
)
def test_heartbeat_recovery_and_liveness_fencing(
    service, runner, workspace, state, desktop, tracked, generation, epoch, active
):
    ws_id = str(workspace.id)
    if desktop and generation == 5:
        assert service.get_desktop_info(ws_id) is None
    else:
        service._record_active_desktop(ws_id, state, runner_id=str(runner.id))
    service.handle_heartbeat(
        runner,
        [
            {
                "workspace_id": ws_id,
                "status": "running",
                "desktop": {**state, "generation": generation, "guest_ip": "changed-ip"}
                if desktop
                else None,
                "desktop_epoch": epoch,
                "desktop_generation": generation,
                "desktop_session_tracked": tracked,
            }
        ],
    )
    assert service.is_desktop_active(ws_id) is active
    if active:
        assert service.get_desktop_info(ws_id) == (
            {**state, "guest_ip": "changed-ip"}
            if desktop and generation == 5
            else state
        )


@pytest.mark.parametrize("operation", ["stopped", "removed"])
def test_workspace_shutdown_cleans_desktop_state(
    service, runner, workspace, state, operation
):
    ws_id = str(workspace.id)
    service._record_active_desktop(ws_id, state, runner_id=str(runner.id))
    task = Task.objects.create(
        runner=runner,
        workspace=workspace,
        type=TaskType.STOP_WORKSPACE
        if operation == "stopped"
        else TaskType.REMOVE_WORKSPACE,
        status=TaskStatus.IN_PROGRESS,
    )
    getattr(service, f"handle_workspace_{operation}")(
        str(task.id), ws_id, runner_id=str(runner.id)
    )
    assert service.get_desktop_info(ws_id) is None
    assert ws_id not in service._desktop_workspace_runner
    workspace.refresh_from_db()
    assert workspace.status == (
        WorkspaceStatus.STOPPED if operation == "stopped" else WorkspaceStatus.DELETED
    )
