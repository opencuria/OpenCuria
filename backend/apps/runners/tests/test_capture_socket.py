"""Frontend capture conflicts complete client requests without bypassing auth."""

from __future__ import annotations

import uuid
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import socketio

from apps.runners import sio_server
from apps.runners.enums import WorkspaceStatus
from apps.runners.services import RunnerService

# These are the completion events consumed by useWorkspaceFileEvents. In
# particular, reads return content_result and upload chunks fail upload_result.
RELAYS = [
    ("terminal_input", "forward_terminal_input", "terminal:error"),
    ("terminal_resize", "forward_terminal_resize", "terminal:error"),
    ("terminal_close", "forward_terminal_close", "terminal:error"),
    ("files_list", "forward_files_event", "files:list_result"),
    ("files_find", "forward_files_event", "files:find_result"),
    ("files_read", "forward_files_event", "files:content_result"),
    ("files_upload", "forward_files_event", "files:upload_result"),
    ("files_upload_start", "forward_files_event", "files:upload_result"),
    ("files_upload_chunk", "forward_files_event", "files:upload_result"),
    ("files_upload_finish", "forward_files_event", "files:upload_result"),
    ("files_download", "forward_files_event", "files:download_result"),
]


@pytest.fixture
def capture_socket(monkeypatch):
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_frontend_handlers(server)
    server.emit = AsyncMock()
    server.get_session = AsyncMock(return_value={"user_id": "123"})
    access = AsyncMock(return_value=True)
    monkeypatch.setattr(sio_server, "_frontend_user_can_access_workspace", access)
    monkeypatch.setattr(sio_server, "_frontend_subscriptions", defaultdict(set))
    monkeypatch.setattr(sio_server, "_frontend_sid_workspaces", defaultdict(set))
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        status=WorkspaceStatus.RUNNING,
        active_operation="capturing_image",
        current_task_id=uuid.uuid4(),
        runner=SimpleNamespace(id=uuid.uuid4(), is_online=True),
    )
    service = RunnerService(sio_server=server)
    service.workspaces = Mock()
    service.workspaces.get_by_id.return_value = workspace
    service._emit_to_runner = AsyncMock()
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    monkeypatch.setattr(sio_server, "get_sio_server", lambda: server)
    return server, service, workspace, access


def request(workspace):
    return {
        "workspace_id": str(workspace.id),
        "request_id": "request-1",
        "upload_id": "upload-1",
        "terminal_id": "terminal-1",
        "path": "/workspace/test.txt",
        "query": "test",
        "data": "echo test",
        "content": "dGVzdA==",
        "index": 0,
        "total_chunks": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("event,method,result_event", RELAYS)
async def test_capture_conflict_completes_request_once(
    capture_socket, event, method, result_event
):
    server, service, workspace, access = capture_socket
    data = request(workspace)
    await server.handlers["/frontend"][f"frontend:{event}"]("caller", data)

    access.assert_awaited_once_with(123, str(workspace.id))
    service._emit_to_runner.assert_not_awaited()
    server.emit.assert_awaited_once()
    args, kwargs = server.emit.await_args
    assert args[0] == result_event
    assert kwargs == {"to": "caller", "namespace": "/frontend"}
    payload = args[1]
    for key in (
        "workspace_id",
        "request_id",
        "upload_id",
        "terminal_id",
        "path",
        "query",
    ):
        assert payload[key] == data[key]
    assert "captur" in payload["error"]
    assert payload["code"] == "conflict"
    if result_event == "files:list_result":
        assert payload["entries"] == []
    elif result_event == "files:find_result":
        assert payload["paths"] == []
        assert payload["truncated"] is False
    elif result_event == "files:content_result":
        assert payload["content"] == ""
        assert payload["size"] == 0
        assert payload["truncated"] is False
    elif result_event == "files:upload_result":
        assert payload["status"] == "error"
    elif result_event == "files:download_result":
        assert payload["content"] == ""
        assert payload["filename"] == ""
        assert payload["is_archive"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("event,method,result_event", RELAYS)
@pytest.mark.parametrize("authenticated", [False, True])
async def test_unauthorized_capture_requests_are_silently_dropped(
    capture_socket, event, method, result_event, authenticated
):
    server, service, workspace, access = capture_socket
    server.get_session.return_value = {"user_id": "123"} if authenticated else {}
    access.return_value = False
    dispatch = AsyncMock()
    service_lookup = Mock(return_value=service)
    # A rejected request must never inspect capture state or enter the service.
    setattr(service, method, dispatch)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sio_server, "get_runner_service", service_lookup)
        await server.handlers["/frontend"][f"frontend:{event}"](
            "caller", request(workspace)
        )
    service_lookup.assert_not_called()
    dispatch.assert_not_awaited()
    service._emit_to_runner.assert_not_awaited()
    server.emit.assert_not_awaited()
    if not authenticated:
        access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event,method,result_event", RELAYS)
async def test_normal_requests_dispatch_once(
    capture_socket, event, method, result_event
):
    server, service, workspace, access = capture_socket
    workspace.active_operation = ""
    workspace.current_task_id = None
    await server.handlers["/frontend"][f"frontend:{event}"](
        "caller", request(workspace)
    )
    access.assert_awaited_once()
    service._emit_to_runner.assert_awaited_once()
    server.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_allows_subscriptions_and_status_delivery(capture_socket):
    server, service, workspace, access = capture_socket
    workspace_id = str(workspace.id)
    data = {"workspace_id": workspace_id}
    handlers = server.handlers["/frontend"]
    await handlers["frontend:subscribe_workspace"]("caller", data)
    assert sio_server._frontend_subscriptions[workspace_id] == {"caller"}
    assert sio_server._frontend_sid_workspaces["caller"] == {workspace_id}
    status = {**data, "status": "running", "active_operation": "capturing_image"}
    await sio_server.emit_to_frontend("workspace:status_changed", status, workspace_id)
    server.emit.assert_awaited_once_with(
        "workspace:status_changed", status, to="caller", namespace="/frontend"
    )
    await handlers["frontend:unsubscribe_workspace"]("caller", data)
    assert workspace_id not in sio_server._frontend_subscriptions
    assert not sio_server._frontend_sid_workspaces["caller"]
    service._emit_to_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_service_errors_are_not_swallowed(capture_socket):
    server, service, workspace, _ = capture_socket
    service.forward_terminal_input = AsyncMock(side_effect=RuntimeError("unexpected"))
    with pytest.raises(RuntimeError, match="unexpected"):
        await server.handlers["/frontend"]["frontend:terminal_input"](
            "caller", request(workspace)
        )
    server.emit.assert_not_awaited()
