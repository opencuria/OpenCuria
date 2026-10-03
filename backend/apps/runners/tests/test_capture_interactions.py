"""Capture's persisted lifecycle hold fences live workspace interactions."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.runners import desktop_proxy
from apps.runners.enums import ProcessStatus, WorkspaceStatus
from apps.runners.exceptions import RunnerOfflineError, WorkspaceStateError
from apps.runners.services import RunnerService
from common.exceptions import ConflictError


@pytest.fixture
def interaction_service():
    service = RunnerService(sio_server=AsyncMock())
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        status=WorkspaceStatus.RUNNING,
        active_operation="capturing_image",
        current_task_id=uuid.uuid4(),
        runner=SimpleNamespace(id=uuid.uuid4(), is_online=True),
    )
    service.workspaces = Mock()
    service.workspaces.get_by_id.return_value = workspace
    service._emit_to_runner = AsyncMock()
    service._call_runner = AsyncMock()
    service.tasks = Mock()
    return service, workspace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("start_terminal", ()),
        ("start_desktop", ()),
        ("stop_desktop", ()),
        ("write_desktop_clipboard", ("clipboard",)),
        ("read_desktop_clipboard", ()),
        ("forward_terminal_input", ("terminal", "echo danger")),
        ("forward_terminal_resize", ("terminal", 120, 40)),
        ("forward_terminal_close", ("terminal",)),
    ],
)
async def test_capture_blocks_interactive_dispatch(interaction_service, method, args):
    service, workspace = interaction_service
    workspace_id = str(workspace.id) if method.startswith("forward") else workspace.id
    with pytest.raises(ConflictError):
        await getattr(service, method)(workspace_id, *args)
    service._emit_to_runner.assert_not_awaited()
    service._call_runner.assert_not_awaited()
    service.tasks.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        "files:read",
        "files:list",
        "files:find",
        "files:upload",
        "files:upload_chunk",
        "files:upload_finish",
        "files:download",
    ],
)
async def test_capture_blocks_every_file_dispatch(interaction_service, event):
    service, workspace = interaction_service
    with pytest.raises(ConflictError):
        await service.forward_files_event(str(workspace.id), event, {})
    service._emit_to_runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "start_process",
        "restart_process",
        "stop_process",
        "delete_process",
        "get_process",
        "list_processes",
    ],
)
async def test_capture_blocks_live_processes(interaction_service, method):
    service, workspace = interaction_service
    process = SimpleNamespace(
        id=uuid.uuid4(),
        status=ProcessStatus.RUNNING,
        kind="persistent",
        workspace=workspace,
        pid=123,
    )
    service._resolve_process = AsyncMock(return_value=process)
    service._await_process_result = AsyncMock()
    args = (
        (workspace.id,)
        if method == "list_processes"
        else (workspace.id, "echo danger" if method == "start_process" else process.id)
    )
    kwargs = {"name": "test"} if method == "start_process" else {}
    with pytest.raises(ConflictError):
        await getattr(service, method)(*args, **kwargs)
    service._await_process_result.assert_not_awaited()
    service._emit_to_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_process_history_is_allowed(interaction_service):
    service, workspace = interaction_service
    service.processes = Mock()
    service.processes.list_by_workspace.return_value = []
    assert await service.list_processes(workspace.id, live=False) == []


def test_process_guard_preserves_state_and_offline_errors(interaction_service):
    service, workspace = interaction_service
    workspace.status = WorkspaceStatus.DELETING
    with pytest.raises(WorkspaceStateError):
        service._ensure_process_dispatchable(workspace)
    workspace.status = WorkspaceStatus.STOPPED
    with pytest.raises(WorkspaceStateError):
        service._ensure_process_dispatchable(workspace)
    workspace.status = WorkspaceStatus.RUNNING
    workspace.active_operation = ""
    workspace.current_task_id = None
    workspace.runner.is_online = False
    with pytest.raises(RunnerOfflineError):
        service._ensure_process_dispatchable(workspace)


@pytest.mark.asyncio
async def test_normal_terminal_input_dispatches(interaction_service):
    service, workspace = interaction_service
    workspace.active_operation = ""
    workspace.current_task_id = None
    await service.forward_terminal_input(str(workspace.id), "term", "ok")
    service._emit_to_runner.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_new_proxy_requests_blocked_after_auth(monkeypatch, scope_type):
    monkeypatch.setattr(
        desktop_proxy, "_validate_token", AsyncMock(return_value=SimpleNamespace(pk=1))
    )
    access = AsyncMock(return_value=True)
    monkeypatch.setattr(desktop_proxy, "_user_can_access_workspace", access)
    monkeypatch.setattr(
        desktop_proxy, "_desktop_workspace_available", AsyncMock(return_value=False)
    )
    target = AsyncMock()
    monkeypatch.setattr(desktop_proxy, "_get_desktop_proxy_target", target)
    send = AsyncMock()
    await desktop_proxy.desktop_proxy_app(
        {
            "type": scope_type,
            "path": f"/ws/desktop/{uuid.uuid4()}/",
            "query_string": b"token=ok",
        },
        AsyncMock(),
        send,
    )
    access.assert_awaited_once()
    target.assert_not_awaited()
    first = send.await_args_list[0].args[0]
    assert first.get("status", first.get("code")) == (
        409 if scope_type == "http" else 4009
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("idle", [False, True])
async def test_existing_tunnel_retires_during_capture(monkeypatch, idle):
    sio = AsyncMock()
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
    monkeypatch.setattr(
        desktop_proxy, "_desktop_workspace_available", AsyncMock(return_value=False)
    )
    receive = AsyncMock(return_value={"type": "websocket.receive", "bytes": b"input"})
    if idle:

        async def receive():
            await asyncio.Event().wait()

    send = AsyncMock()
    await asyncio.wait_for(
        desktop_proxy._ws_proxy_loop(
            receive,
            send,
            tunnel_id="tunnel",
            runner_sid="runner",
            queue=asyncio.Queue(),
            workspace_id=str(uuid.uuid4()),
        ),
        timeout=1,
    )
    assert all(
        call.args[0] != "desktop:proxy_ws_send" for call in sio.emit.await_args_list
    )
    sio.emit.assert_awaited_once_with(
        "desktop:proxy_ws_close", {"tunnel_id": "tunnel"}, to="runner"
    )
    assert any(
        call.args[0] == {"type": "websocket.close", "code": 4009}
        for call in send.await_args_list
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_proxy_gate_reads_persisted_capture_hold(workspace):
    workspace.active_operation = "capturing_image"
    workspace.save(update_fields=["active_operation"])
    assert not await desktop_proxy._desktop_workspace_available(str(workspace.id))
    workspace.active_operation = ""
    workspace.save(update_fields=["active_operation"])
    assert await desktop_proxy._desktop_workspace_available(str(workspace.id))


@pytest.mark.asyncio
async def test_internal_session_cleanup_can_settle_during_capture(interaction_service):
    service, workspace = interaction_service
    row = SimpleNamespace(id=uuid.uuid4())
    service.processes = Mock()
    service.processes.list_running_session_processes.return_value = [row]
    service._stop_running_process = AsyncMock(return_value=row)
    assert await service.stop_session_processes(workspace.id, uuid.uuid4()) == [row]
    service._stop_running_process.assert_awaited_once_with(workspace, row)


@pytest.mark.asyncio
async def test_idle_tunnel_detects_capture_acquired_after_connection(monkeypatch):
    sio = AsyncMock()
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
    availability = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(desktop_proxy, "_desktop_workspace_available", availability)

    async def receive():
        await asyncio.Event().wait()

    send = AsyncMock()
    await asyncio.wait_for(
        desktop_proxy._ws_proxy_loop(
            receive,
            send,
            tunnel_id="tunnel",
            runner_sid="runner",
            queue=asyncio.Queue(),
            workspace_id=str(uuid.uuid4()),
        ),
        timeout=1,
    )
    assert availability.await_count == 2
    send.assert_awaited_once_with({"type": "websocket.close", "code": 4009})
    sio.emit.assert_awaited_once_with(
        "desktop:proxy_ws_close", {"tunnel_id": "tunnel"}, to="runner"
    )


@pytest.mark.asyncio
async def test_file_runner_results_not_blocked_during_capture(
    interaction_service, monkeypatch
):
    service, workspace = interaction_service
    service._route_harness_reply = Mock(return_value=None)
    emit = AsyncMock()
    monkeypatch.setattr("apps.runners.sio_server.emit_to_frontend", emit)
    payload = {"workspace_id": str(workspace.id), "entries": []}
    await service.handle_files_result("files:list_result", payload)
    emit.assert_awaited_once_with("files:list_result", payload, str(workspace.id))


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [True, False])
async def test_file_normal_dispatch_and_offline_error(
    interaction_service, monkeypatch, online
):
    service, workspace = interaction_service
    workspace.active_operation = ""
    workspace.current_task_id = None
    workspace.runner.is_online = online
    emit = AsyncMock()
    monkeypatch.setattr("apps.runners.sio_server.emit_to_frontend", emit)
    await service.forward_files_event(
        str(workspace.id), "files:download", {"request_id": "read", "path": "/x"}
    )
    if online:
        service._emit_to_runner.assert_awaited_once()
        emit.assert_not_awaited()
    else:
        service._emit_to_runner.assert_not_awaited()
        assert emit.await_args.args[0] == "files:download_result"
        assert emit.await_args.args[1]["error"] == "Runner is offline"
