"""Terminal sessions are bounded by the runner, never by client goodwill."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.runtime.base import PtyHandle
from src.service import WorkspaceService
from src.services.sessions import terminals as terminals_module
from src.services.sessions.terminals import TerminalManager
from tests.command_support import make_interface


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _PtyRuntime:
    """Fake PTY runtime whose reads block until the PTY is closed."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.output: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: list[bytes] = []

    async def workspace_exists(self, instance_id: str) -> bool:
        return True

    async def exec_pty(self, instance_id: str, **kwargs) -> PtyHandle:
        handle = PtyHandle(instance_id=instance_id, handle=object())
        self.events.append(("open", handle))
        return handle

    async def pty_read(self, handle: PtyHandle, size: int = 4096) -> bytes:
        while not handle.closed:
            try:
                return await asyncio.wait_for(self.output.get(), 0.01)
            except asyncio.TimeoutError:
                continue
        return b""

    async def pty_write(self, handle: PtyHandle, data: bytes) -> None:
        self.written.append(data)

    async def pty_resize(self, handle: PtyHandle, cols: int, rows: int) -> None:
        return None

    async def pty_close(self, handle: PtyHandle) -> None:
        handle.closed = True
        self.events.append(("close", handle))


def _manager(
    runtime: _PtyRuntime,
    clock: _Clock,
    *workspace_ids: uuid.UUID,
    **limits,
) -> tuple[TerminalManager, list[tuple[str, uuid.UUID | None, str]]]:
    cache = {
        workspace_id: WorkspaceInfo(
            workspace_id=workspace_id, instance_id="guest", status="running"
        )
        for workspace_id in workspace_ids
    }
    manager = TerminalManager(
        runtime,
        cache.__getitem__,
        lambda _: runtime,
        clock=clock,
        **{"lease_timeout": 90, "idle_timeout": 3600, **limits},
    )
    closed: list[tuple[str, uuid.UUID | None, str]] = []

    async def on_closed(terminal_id, workspace_id, reason) -> None:
        closed.append((terminal_id, workspace_id, reason))

    manager.on_closed = on_closed
    return manager, closed


async def test_unrenewed_terminal_closes_as_detached() -> None:
    """A client that vanished without closing never keeps its PTY."""
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws)
    terminal_id = await manager.start_terminal(ws)

    clock.now += 89
    await manager.expire_terminals()
    assert closed == []

    clock.now += 1
    await manager.expire_terminals()
    assert closed == [(terminal_id, ws, "detached")]
    assert [event for event, _ in runtime.events] == ["open", "close"]
    assert not manager.belongs_to(terminal_id, ws)


async def test_renewal_keeps_attached_terminals_and_reports_unknown() -> None:
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws)
    terminal_id = await manager.start_terminal(ws)
    foreign = uuid.uuid4()

    clock.now += 80
    unknown = manager.renew_terminals(
        [(terminal_id, ws), (terminal_id, foreign), ("gone", ws)]
    )
    assert unknown == [(terminal_id, foreign), ("gone", ws)]

    clock.now += 80
    await manager.expire_terminals()
    assert closed == []
    assert manager.belongs_to(terminal_id, ws)


async def test_unused_terminal_closes_as_idle_despite_renewals() -> None:
    """An attached but forgotten shell is closed after the idle timeout."""
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws, idle_timeout=600)
    used = await manager.start_terminal(ws)
    unused = await manager.start_terminal(ws)

    for _ in range(4):
        clock.now += 150
        manager.renew_terminals([(used, ws), (unused, ws)])
        await manager.write_terminal(used, b"ls\n")
        await manager.expire_terminals()

    assert closed == [(unused, ws, "idle")]
    assert manager.belongs_to(used, ws)


async def test_output_counts_as_use() -> None:
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws, idle_timeout=600)
    terminal_id = await manager.start_terminal(ws)
    reader = manager.read_terminal(terminal_id)

    clock.now += 599
    await runtime.output.put(b"build step\n")
    assert await anext(reader) == b"build step\n"
    clock.now += 599
    manager.renew_terminals([(terminal_id, ws)])
    await manager.expire_terminals()

    assert closed == []
    await reader.aclose()


async def test_zero_idle_timeout_disables_idle_expiry() -> None:
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws, idle_timeout=0)
    terminal_id = await manager.start_terminal(ws)

    clock.now += 86_400
    manager.renew_terminals([(terminal_id, ws)])
    await manager.expire_terminals()

    assert closed == []


async def test_cap_closes_least_recently_used_terminal_before_opening() -> None:
    """The new PTY never needs a guest session beyond the per-workspace cap."""
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    other_ws = uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws, other_ws, max_per_workspace=2)
    first = await manager.start_terminal(ws)
    clock.now += 1
    second = await manager.start_terminal(ws)
    other = await manager.start_terminal(other_ws)
    clock.now += 1
    await manager.resize_terminal(first, 100, 30)

    clock.now += 1
    third = await manager.start_terminal(ws)

    assert closed == [(second, ws, "evicted")]
    second_handle = runtime.events[1][1]
    assert runtime.events[-2:] == [("close", second_handle), runtime.events[-1]]
    assert runtime.events[-1][0] == "open"
    assert manager.belongs_to(first, ws)
    assert manager.belongs_to(third, ws)
    assert manager.belongs_to(other, other_ws)


async def test_close_releases_once_and_notifies_once() -> None:
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws)
    terminal_id = await manager.start_terminal(ws)

    assert await manager.close_terminal(terminal_id, "closed") is True
    assert await manager.close_terminal(terminal_id, "exited") is False
    clock.now += 1000
    await manager.expire_terminals()

    assert closed == [(terminal_id, ws, "closed")]
    assert [event for event, _ in runtime.events] == ["open", "close"]


async def test_unacknowledged_pty_close_does_not_stall(monkeypatch) -> None:
    runtime, clock, ws = _PtyRuntime(), _Clock(), uuid.uuid4()
    manager, closed = _manager(runtime, clock, ws)
    terminal_id = await manager.start_terminal(ws)
    hung = asyncio.Event()

    async def never_acknowledged(handle: PtyHandle) -> None:
        await hung.wait()

    runtime.pty_close = never_acknowledged
    monkeypatch.setattr(terminals_module, "_PTY_CLOSE_TIMEOUT", 0.01)

    assert await asyncio.wait_for(manager.close_terminal(terminal_id), 1) is True
    assert closed == [(terminal_id, ws, "closed")]


# -- Socket.IO adapter ---------------------------------------------------------


@pytest.fixture
def interface(tmp_path):
    runtime = _PtyRuntime()
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    ws = uuid.uuid4()
    service.registry._cache[ws] = WorkspaceInfo(
        workspace_id=ws, instance_id="guest", status="running"
    )
    interface = make_interface(service, RunnerSettings(state_dir=str(tmp_path)))
    interface._sio.emit = AsyncMock()
    interface._sio.connected = True
    yield interface, service, runtime, ws
    for task in interface._running_tasks.values():
        task.cancel()


def _emitted(interface, event: str) -> list[dict]:
    return [
        call.args[1]
        for call in interface._sio.emit.await_args_list
        if call.args[0] == event
    ]


async def _start(interface, ws: uuid.UUID) -> str:
    await interface._sio.handlers["/"]["task:start_terminal"](
        {"task_id": "task", "workspace_id": str(ws)}
    )
    (started,) = _emitted(interface, "terminal:started")
    return started["terminal_id"]


async def test_backend_close_stops_reader_and_reports_once(interface) -> None:
    interface, _, _, ws = interface
    terminal_id = await _start(interface, ws)
    reader = interface._running_tasks[f"terminal:{terminal_id}"]

    await interface._sio.handlers["/"]["terminal:close"](
        {"workspace_id": str(ws), "terminal_id": terminal_id}
    )
    await asyncio.gather(reader, return_exceptions=True)

    assert _emitted(interface, "terminal:closed") == [
        {"workspace_id": str(ws), "terminal_id": terminal_id, "reason": "closed"}
    ]
    assert f"terminal:{terminal_id}" not in interface._running_tasks


async def test_shell_exit_reports_exited_once(interface) -> None:
    interface, _, runtime, ws = interface
    terminal_id = await _start(interface, ws)
    reader = interface._running_tasks[f"terminal:{terminal_id}"]

    runtime.events[0][1].closed = True  # The guest ended the shell.
    await asyncio.wait_for(reader, 1)

    assert _emitted(interface, "terminal:closed") == [
        {"workspace_id": str(ws), "terminal_id": terminal_id, "reason": "exited"}
    ]
    assert interface._running_tasks == {}


async def test_renewal_reports_unknown_terminals_closed(interface) -> None:
    """Routes left behind by lost close events are reconciled."""
    interface, service, _, ws = interface
    terminal_id = await _start(interface, ws)

    await interface._sio.handlers["/"]["terminal:renew"](
        {
            "terminals": [
                {"terminal_id": terminal_id, "workspace_id": str(ws)},
                {"terminal_id": "gone", "workspace_id": str(ws)},
                {"terminal_id": "malformed"},
            ]
        }
    )

    assert _emitted(interface, "terminal:closed") == [
        {"workspace_id": str(ws), "terminal_id": "gone", "reason": "exited"}
    ]
    assert service.terminal_manager.belongs_to(terminal_id, ws)


async def test_input_to_foreign_or_unknown_terminal_is_refused(interface) -> None:
    interface, service, runtime, ws = interface
    terminal_id = await _start(interface, ws)
    other = str(uuid.uuid4())

    await interface._sio.handlers["/"]["terminal:input"](
        {"workspace_id": other, "terminal_id": terminal_id, "data": "bHMK"}
    )
    await interface._sio.handlers["/"]["terminal:input"](
        {"workspace_id": str(ws), "terminal_id": terminal_id, "data": "bHMK"}
    )

    assert runtime.written == [b"ls\n"]
    assert _emitted(interface, "terminal:closed") == [
        {"workspace_id": other, "terminal_id": terminal_id, "reason": "exited"}
    ]
    assert service.terminal_manager.belongs_to(terminal_id, ws)


async def test_expiry_runs_while_backend_is_unreachable(interface) -> None:
    interface, service, _, ws = interface
    terminal_id = await _start(interface, ws)
    interface._sio.connected = False
    interface._sio.emit.reset_mock()
    entry = service.terminal_manager._terminals[terminal_id]
    entry.lease_expires_at = 0

    await service.terminal_manager.expire_terminals()

    assert not service.terminal_manager.belongs_to(terminal_id, ws)
    interface._sio.emit.assert_not_awaited()
