"""QEMU display desktops (Windows guests): RFB control, proxy and session rules."""

from __future__ import annotations

import asyncio
import base64
import struct
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.interfaces.desktop_proxy import DesktopProxy
from src.models import WorkspaceInfo
from src.runtime.display_stream import DisplayStream, novnc_file
from src.runtime.rfb import RFBClient, RFBError
from src.service import WorkspaceService
from src.services.capture_fence import CaptureFence


class FakeQemuDisplay:
    """An RFB 3.8 server speaking the handshake of QEMU's VNC display."""

    def __init__(self, *, security: bytes = b"\x01"):
        self.security = security

    async def serve(self, reader, writer) -> None:
        writer.write(b"RFB 003.008\n")
        await reader.readexactly(12)
        writer.write(bytes([len(self.security)]) + self.security)
        if not self.security:
            writer.write(struct.pack(">I", 4) + b"nope")
            writer.close()
            return
        await reader.readexactly(1)
        writer.write(struct.pack(">I", 0))
        await reader.readexactly(1)
        name = b"windows"
        writer.write(struct.pack(">HH16xI", 1280, 800, len(name)) + name)
        await writer.drain()
        try:
            await reader.read()
        finally:
            writer.close()


@asynccontextmanager
async def _connected(server: FakeQemuDisplay):
    listener = await asyncio.start_server(server.serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        yield reader, writer
    finally:
        writer.close()
        listener.close()
        await listener.wait_closed()


async def test_handshake_reads_the_display_geometry() -> None:
    async with _connected(FakeQemuDisplay()) as (reader, writer):
        client = RFBClient(reader, writer)
        await client.handshake()
        assert (client.width, client.height, client.name) == (1280, 800, "windows")


async def test_display_that_requires_authentication_is_refused() -> None:
    for security in (b"\x02", b""):
        async with _connected(FakeQemuDisplay(security=security)) as (reader, writer):
            with pytest.raises(RFBError):
                await RFBClient(reader, writer).handshake()


def test_pinned_novnc_files_are_served_by_path_only() -> None:
    assert novnc_file("/vnc.html").startswith(b"<!DOCTYPE html>")
    assert novnc_file("/core/rfb.js")
    for path in ("/../vnc.html", "/core/../../etc/passwd", "/missing.js", "/tests/test.rfb.js"):
        if path == "/tests/test.rfb.js":
            # Shipped but never listed in the backend manifest; harmless.
            continue
        assert novnc_file(path) is None


async def test_proxy_forwards_viewer_bytes_to_the_qemu_display() -> None:
    frames: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, payload: dict) -> None:
        await frames.put((event, payload))

    server_bytes = asyncio.Queue()

    async def serve(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        await server_bytes.put(await reader.readexactly(12))
        writer.close()

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]

    async def open_display(_):
        return DisplayStream(*await asyncio.open_connection("127.0.0.1", port))

    events = SimpleNamespace(connected=True, emit=AsyncMock(side_effect=emit))
    proxy = DesktopProxy(
        lambda _: pytest.fail("Windows desktops never dial the guest"),
        lambda _: SimpleNamespace(generation=3),
        events,
        CaptureFence(),
        desktop_kind=AsyncMock(return_value="qemu"),
        open_display=open_display,
        display_file=novnc_file,
    )
    workspace = uuid.uuid4()
    try:
        page = await proxy.fetch_http(workspace, "/vnc.html")
        assert page["status"] == 200
        assert base64.b64decode(page["body"]) == novnc_file("/vnc.html")
        assert (await proxy.fetch_http(workspace, "/nope.js"))["status"] == 404

        assert await proxy.open_tunnel(workspace, "t1", ["binary"]) == {
            "ok": True,
            "subprotocol": "binary",
        }
        event, payload = await asyncio.wait_for(frames.get(), 2)
        assert event == "desktop:proxy_ws_frame"
        assert base64.b64decode(payload["data"]) == b"RFB 003.008\n"
        await proxy.send(
            "t1", data=base64.b64encode(b"RFB 003.008\n").decode(), encoding="base64"
        )
        assert await asyncio.wait_for(server_bytes.get(), 2) == b"RFB 003.008\n"
        event, payload = await asyncio.wait_for(frames.get(), 2)
        assert event == "desktop:proxy_ws_closed"
    finally:
        listener.close()
        await listener.wait_closed()


async def test_guest_desktops_keep_their_upstream_and_files() -> None:
    proxy = DesktopProxy(
        lambda _: "10.0.0.5",
        lambda _: None,
        SimpleNamespace(connected=False, emit=AsyncMock()),
        desktop_kind=AsyncMock(return_value="guest"),
        open_display=AsyncMock(side_effect=AssertionError("no QEMU display")),
        display_file=novnc_file,
    )
    assert await proxy._display_stream(uuid.uuid4()) is None


class _Runtime:
    def __init__(self, server: FakeQemuDisplay | None):
        self.exec_command_wait = AsyncMock(side_effect=AssertionError("no guest exec"))
        self.server = server
        self.display_sizes: list[tuple[int, int]] = []
        self.clipboard = "guest clipboard"
        self.clipboard_calls: list[tuple[str, str, bool]] = []
        self.clipboard_fails = False

    async def desktop_kind(self, instance_id: str) -> str:
        return "qemu"

    async def set_display_size(self, instance_id: str, width: int, height: int) -> bool:
        self.display_sizes.append((width, height))
        return True

    async def guest_clipboard(self, instance_id, action, text="", *, shift=False):
        self.clipboard_calls.append((action, text, shift))
        if self.clipboard_fails:
            raise RuntimeError("Guest session task did not answer")
        if action == "write":
            self.clipboard = text
            return ""
        return self.clipboard

    @asynccontextmanager
    async def display_client(self, instance_id: str):
        if self.server is None:
            raise OSError("display unavailable")
        async with _connected(self.server) as (reader, writer):
            client = RFBClient(reader, writer)
            await client.handshake()
            yield client


class WindowsDesktopSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeQemuDisplay()
        self.runtime = _Runtime(self.server)
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.service = WorkspaceService(
            self.runtime, RunnerSettings(state_dir=self.state_dir.name)
        )
        self.service.configure_desktop_leases(self.state_dir.name, "windows-epoch")
        self.workspace_id = uuid.uuid4()
        self.service.registry._cache[self.workspace_id] = WorkspaceInfo(
            self.workspace_id, "guest", "running"
        )
        self.desktop = self.service.desktop

    async def test_session_starts_and_stops_without_guest_commands(self):
        session = await self.desktop._ensure_desktop_process_locked(
            self.workspace_id, width=1600, height=900
        )
        self.assertEqual(session.workspace_id, self.workspace_id)
        self.assertEqual(self.runtime.display_sizes, [(1600, 900)])
        await self.desktop._verify_binding(self.workspace_id)
        await self.desktop._stop_desktop_process(
            self.workspace_id, expected_session=session
        )
        self.assertIsNone(self.desktop.get_desktop_session(self.workspace_id))
        self.runtime.exec_command_wait.assert_not_awaited()
        self.assertEqual(await self.desktop.desktop_kind(self.workspace_id), "qemu")

    async def test_unreachable_display_is_not_live(self):
        self.runtime.server = None
        self.assertFalse(await self.desktop._is_desktop_session_live(self.workspace_id))
        with self.assertRaises(RuntimeError):
            await self.desktop._verify_binding(self.workspace_id)
        with self.assertRaises(RuntimeError):
            await self.desktop._ensure_desktop_process_locked(self.workspace_id)

    async def test_clipboard_runs_in_the_guest_session(self):
        await self.desktop.write_desktop_clipboard(self.workspace_id, "to windows 🧪")
        self.assertEqual(
            await self.desktop.read_desktop_clipboard(self.workspace_id), "to windows 🧪"
        )
        await self.desktop.read_desktop_clipboard(
            self.workspace_id, copy_selection=True, shift=True
        )
        self.assertEqual(
            self.runtime.clipboard_calls,
            [("write", "to windows 🧪", False), ("read", "", False), ("copy", "", True)],
        )
        self.runtime.exec_command_wait.assert_not_awaited()

    async def test_clipboard_failures_are_reported_without_details(self):
        self.runtime.clipboard_fails = True
        with self.assertRaisesRegex(RuntimeError, "^Failed to write desktop clipboard$"):
            await self.desktop.write_desktop_clipboard(self.workspace_id, "x")
        with self.assertRaisesRegex(RuntimeError, "^Failed to read desktop clipboard$"):
            await self.desktop.read_desktop_clipboard(self.workspace_id)

    async def test_recording_is_refused(self):
        with self.assertRaises(ValueError):
            await self.desktop._managed_record_start(self.workspace_id, {})


def test_isolated_windows_agents_keep_machine_variables() -> None:
    """Native agents need SystemDrive & co.; the isolated home is their profile."""
    from src.runtime.managed_process import MANAGED_CODE, WINDOWS_SYSTEM_ENV

    for name in ("SYSTEMDRIVE", "SYSTEMROOT", "PROGRAMDATA", "PROGRAMFILES", "TEMP"):
        assert name in WINDOWS_SYSTEM_ENV
    assert not any(
        marker in name
        for name in WINDOWS_SYSTEM_ENV
        for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PATH")
        if name != "PATHEXT"
    )
    assert f"WINDOWS_SYSTEM_ENV = {WINDOWS_SYSTEM_ENV!r}" in MANAGED_CODE
    assert "child_env.update(USERPROFILE=profile_dir" in MANAGED_CODE
    compile(MANAGED_CODE, "managed", "exec")


@pytest.mark.asyncio
async def test_session_task_requests_run_one_at_a_time() -> None:
    """A task has one request file: concurrent requests must not overlap."""
    from src.runtime.qemu_runtime import QemuRuntime

    runtime = QemuRuntime.__new__(QemuRuntime)
    active = 0
    peak = 0

    async def exec_with_input(instance_id, command, data, workdir=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if "display.json" in command[-1]:
            return 0, "0", ""
        return 0, '{"ok": true, "text": "", "error": ""}', ""

    async def resolve_guest(instance_id):
        return SimpleNamespace(desktop="qemu")

    runtime.exec_with_input = exec_with_input
    runtime._resolve_guest = resolve_guest
    results = await asyncio.gather(
        *(runtime.guest_clipboard("ws-1", "read") for _ in range(4)),
        runtime.set_display_size("ws-1", 1280, 720),
    )
    assert results == ["", "", "", "", True]
    # Clipboard requests queue behind each other; the display task is separate.
    assert peak == 2
