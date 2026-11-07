"""Terminals are bound to the client using them, never left to its goodwill."""

from __future__ import annotations

import uuid
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import socketio

from apps.runners import sio_server
from apps.runners.enums import WorkspaceStatus
from apps.runners.services import RunnerRepositories, RunnerService
from common import realtime

USER = "user-1"
OTHER_USER = "user-2"


@pytest.fixture
def frontends(monkeypatch):
    """Isolate the frontend registry and capture room publications."""
    monkeypatch.setattr(realtime, "_frontend_subscriptions", defaultdict(set))
    monkeypatch.setattr(realtime, "_frontend_sid_workspaces", defaultdict(set))
    monkeypatch.setattr(realtime, "_frontend_users", {})
    server = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr(realtime, "get_sio_server", lambda: server)
    return server


@pytest.fixture
def terminals(frontends):
    runner = SimpleNamespace(id=uuid.uuid4(), is_online=True, sid="runner-sid")
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        status=WorkspaceStatus.RUNNING,
        active_operation="",
        current_task_id=None,
        runner=runner,
    )
    service = RunnerService(
        sio_server=AsyncMock(),
        repositories=RunnerRepositories(workspaces=Mock(), tasks=Mock()),
    )
    service.workspaces.get_by_id.return_value = workspace
    service.tasks.get_by_id.return_value = None
    service.tasks.create.side_effect = lambda **kwargs: SimpleNamespace(
        id=kwargs["task_id"]
    )
    service.runner_transport.emit_to_runner = AsyncMock()
    return service, workspace, frontends


def _connect(sid: str, user_id: str, workspace_id: str | None = None) -> None:
    realtime.register_frontend(sid, user_id)
    if workspace_id is not None:
        realtime.subscribe_frontend(sid, workspace_id)


async def _open(service, workspace, *, client_id=None, user_id=USER, terminal="t1"):
    task = await service.interactive_sessions.start_terminal(
        workspace.id, user_id=user_id, client_id=client_id
    )
    await service.interactive_sessions.handle_terminal_started(
        str(task.id), str(workspace.id), terminal, str(workspace.runner.id)
    )
    return terminal


def _runner_events(service) -> list[tuple[str, dict]]:
    return [
        (call.args[1], call.args[2])
        for call in service.runner_transport.emit_to_runner.await_args_list
        if call.args[1] != "task:start_terminal"
    ]


@pytest.mark.asyncio
async def test_disconnect_of_the_requesting_client_closes_its_terminal(terminals):
    service, workspace, _ = terminals
    workspace_id = str(workspace.id)
    _connect("tab-a", USER, workspace_id)
    _connect("tab-b", USER, workspace_id)
    await _open(service, workspace, client_id="tab-a")

    await service.interactive_sessions.release_frontend_terminals("tab-b")
    assert _runner_events(service) == []

    await service.interactive_sessions.release_frontend_terminals("tab-a")
    assert _runner_events(service) == [
        ("terminal:close", {"workspace_id": workspace_id, "terminal_id": "t1"})
    ]
    assert service.session_store.state.terminals == {}


@pytest.mark.asyncio
async def test_leaving_a_workspace_releases_only_its_terminals(terminals):
    service, workspace, _ = terminals
    workspace_id = str(workspace.id)
    _connect("tab", USER, workspace_id)
    await _open(service, workspace, client_id="tab")

    await service.interactive_sessions.release_frontend_terminals(
        "tab", str(uuid.uuid4())
    )
    assert _runner_events(service) == []

    await service.interactive_sessions.release_frontend_terminals("tab", workspace_id)
    assert [event for event, _ in _runner_events(service)] == ["terminal:close"]


@pytest.mark.asyncio
async def test_without_client_id_the_requesters_watching_clients_own_it(terminals):
    service, workspace, _ = terminals
    workspace_id = str(workspace.id)
    _connect("mine", USER, workspace_id)
    _connect("theirs", OTHER_USER, workspace_id)
    await _open(service, workspace)

    route = service.session_store.state.terminals["t1"]
    assert route.owners == {"mine"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_id,client_user",
    [("gone", None), ("foreign", OTHER_USER)],
    ids=["disconnected-client", "foreign-client"],
)
async def test_a_terminal_nobody_can_use_is_closed_at_once(
    terminals, client_id, client_user
):
    """A client that left before the PTY started cannot leak it.

    Other tabs of the requester never adopt it, so they do not own it either.
    """
    service, workspace, frontends = terminals
    workspace_id = str(workspace.id)
    _connect("other-tab", USER, workspace_id)
    if client_user:
        _connect(client_id, client_user, workspace_id)

    await _open(service, workspace, client_id=client_id)

    assert _runner_events(service) == [
        ("terminal:close", {"workspace_id": workspace_id, "terminal_id": "t1"})
    ]
    assert service.session_store.state.terminals == {}
    (event, payload), kwargs = frontends.emit.await_args
    assert event == "workspace:error"
    assert payload["workspace_id"] == workspace_id
    assert kwargs["to"] == f"workspace:{workspace_id}"


@pytest.mark.asyncio
async def test_heartbeat_renews_only_attached_terminals_of_that_runner(terminals):
    service, workspace, _ = terminals
    workspace_id = str(workspace.id)
    _connect("tab", USER, workspace_id)
    await _open(service, workspace, client_id="tab", terminal="kept")
    _connect("closing", USER, workspace_id)
    await _open(service, workspace, client_id="closing", terminal="released")
    await service.interactive_sessions.release_frontend_terminals("closing")
    service.runner_transport.emit_to_runner.reset_mock()

    await service.interactive_sessions.renew_terminal_leases(workspace.runner)
    await service.interactive_sessions.renew_terminal_leases(
        SimpleNamespace(id=uuid.uuid4())
    )

    service.runner_transport.emit_to_runner.assert_awaited_once_with(
        workspace.runner,
        "terminal:renew",
        {"terminals": [{"terminal_id": "kept", "workspace_id": workspace_id}]},
    )


@pytest.mark.asyncio
async def test_runner_close_drops_route_and_forwards_reason(terminals):
    service, workspace, frontends = terminals
    workspace_id = str(workspace.id)
    _connect("tab", USER, workspace_id)
    await _open(service, workspace, client_id="tab")

    await service.interactive_sessions.handle_terminal_closed(
        workspace_id, "t1", str(uuid.uuid4()), reason="idle"
    )
    assert "t1" in service.session_store.state.terminals

    await service.interactive_sessions.handle_terminal_closed(
        workspace_id, "t1", str(workspace.runner.id), reason="idle"
    )
    assert service.session_store.state.terminals == {}
    frontends.emit.assert_awaited_with(
        "terminal:closed",
        {"workspace_id": workspace_id, "terminal_id": "t1", "reason": "idle"},
        to=f"workspace:{workspace_id}",
        namespace="/frontend",
    )


@pytest.mark.asyncio
async def test_client_close_stops_renewal_immediately(terminals):
    service, workspace, _ = terminals
    workspace_id = str(workspace.id)
    _connect("tab", USER, workspace_id)
    await _open(service, workspace, client_id="tab")

    await service.interactive_sessions.forward_terminal_close(workspace_id, "t1")
    service.runner_transport.emit_to_runner.reset_mock()
    await service.interactive_sessions.renew_terminal_leases(workspace.runner)

    service.runner_transport.emit_to_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_runner_does_not_break_release(terminals):
    """The runner's lease expiry closes what the backend cannot reach."""
    service, workspace, _ = terminals
    _connect("tab", USER, str(workspace.id))
    await _open(service, workspace, client_id="tab")
    service.runner_transport.emit_to_runner.side_effect = RuntimeError("offline")

    await service.interactive_sessions.release_frontend_terminals("tab")

    assert service.session_store.state.terminals == {}


# -- Socket.IO adapter ---------------------------------------------------------


@pytest.mark.asyncio
async def test_socket_disconnect_and_unsubscribe_release_terminals(
    frontends, monkeypatch
):
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_frontend_handlers(server)
    server.leave_room = AsyncMock()
    server.get_session = AsyncMock(return_value={"user_id": "123"})
    monkeypatch.setattr(
        sio_server, "_frontend_user_can_access_workspace", AsyncMock(return_value=True)
    )
    service = SimpleNamespace(
        interactive_sessions=SimpleNamespace(release_frontend_terminals=AsyncMock())
    )
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    _connect("tab", "123", "workspace")
    handlers = server.handlers["/frontend"]

    await handlers["frontend:unsubscribe_workspace"](
        "tab", {"workspace_id": "workspace"}
    )
    await handlers["disconnect"]("tab")

    releases = service.interactive_sessions.release_frontend_terminals.await_args_list
    assert [call.args for call in releases] == [("tab", "workspace"), ("tab",)]
    assert realtime.frontend_user("tab") is None
