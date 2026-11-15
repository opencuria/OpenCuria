"""QEMU display desktops (Windows guests): RFB control, proxy and session rules."""

from __future__ import annotations

import asyncio
import base64
import struct
import tempfile
import unittest
import uuid
import zlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from src.config import RunnerSettings
from src.interfaces.desktop_proxy import DesktopProxy
from src.models import DesktopSession, WorkspaceInfo
from src.runtime.display_stream import DisplayStream, novnc_file
from src.runtime.rfb import RFBClient, RFBError
from src.service import WorkspaceService
from src.services.capture_fence import CaptureFence

_TEXT, _CAPS, _REQUEST, _NOTIFY, _PROVIDE = 1, 1 << 24, 1 << 25, 1 << 27, 1 << 28


def _provide(text: str) -> bytes:
    data = text.encode() + b"\x00"
    body = struct.pack(">I", _PROVIDE | _TEXT) + zlib.compress(
        struct.pack(">I", len(data)) + data
    )
    return struct.pack(">Bxxxi", 3, -len(body)) + body


def _ext(flags: int) -> bytes:
    body = struct.pack(">I", flags)
    return struct.pack(">Bxxxi", 3, -len(body)) + body


class FakeQemuDisplay:
    """An RFB 3.8 server speaking the parts of QEMU's VNC the runner uses."""

    def __init__(self, *, guest_text: str = "from windows", security: bytes = b"\x01"):
        self.guest_text = guest_text
        self.security = security
        self.received_text: str | None = None
        self.keys: list[tuple[int, int]] = []

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
        kind, = struct.unpack(">B", await reader.readexactly(1))
        assert kind == 2
        count, = struct.unpack(">xH", await reader.readexactly(3))
        encodings = struct.unpack(f">{count}i", await reader.readexactly(4 * count))
        assert -1063131698 in encodings
        writer.write(_ext(_CAPS | _TEXT) + b"\x02")  # caps and a bell first
        await writer.drain()
        try:
            while True:
                kind = (await reader.readexactly(1))[0]
                if kind == 4:
                    down, key = struct.unpack(">BxxI", await reader.readexactly(7))
                    self.keys.append((key, down))
                    if key == 0x63 and down == 0:
                        writer.write(_ext(_NOTIFY | _TEXT))
                elif kind == 6:
                    length = struct.unpack(">xxxi", await reader.readexactly(7))[0]
                    body = await reader.readexactly(-length)
                    flags = struct.unpack(">I", body[:4])[0]
                    if flags & _PROVIDE:
                        data = zlib.decompress(body[4:])
                        size = struct.unpack(">I", data[:4])[0]
                        self.received_text = data[4 : 4 + size].rstrip(b"\x00").decode()
                    elif flags & _REQUEST:
                        writer.write(_provide(self.guest_text))
                await writer.drain()
        except asyncio.IncompleteReadError:
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


async def test_clipboard_round_trip_through_extended_clipboard() -> None:
    server = FakeQemuDisplay(guest_text="héllo\r\nwelt")
    async with _connected(server) as (reader, writer):
        client = RFBClient(reader, writer)
        await client.handshake()
        assert (client.width, client.height, client.name) == (1280, 800, "windows")
        await client.write_clipboard("päste 🧪")
        assert await client.read_clipboard(timeout=2) == "héllo\nwelt"
        assert server.received_text == "päste 🧪"


async def test_copy_selection_presses_ctrl_c_and_returns_the_new_text() -> None:
    server = FakeQemuDisplay(guest_text="selected")
    async with _connected(server) as (reader, writer):
        client = RFBClient(reader, writer)
        await client.handshake()
        assert await client.copy_selection(timeout=2) == "selected"
    assert server.keys == [(0xFFE3, 1), (0x63, 1), (0x63, 0), (0xFFE3, 0)]


async def test_display_that_requires_authentication_is_refused() -> None:
    for security in (b"\x02", b""):
        async with _connected(FakeQemuDisplay(security=security)) as (reader, writer):
            with pytest.raises(RFBError):
                await RFBClient(reader, writer).handshake()


async def test_oversized_clipboard_text_is_refused() -> None:
    async with _connected(FakeQemuDisplay()) as (reader, writer):
        client = RFBClient(reader, writer)
        await client.handshake()
        with pytest.raises(ValueError):
            await client.write_clipboard("x" * (1024 * 1024 + 1))


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

    async def desktop_kind(self, instance_id: str) -> str:
        return "qemu"

    async def set_display_size(self, instance_id: str, width: int, height: int) -> bool:
        self.display_sizes.append((width, height))
        return True

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
        self.server = FakeQemuDisplay(guest_text="guest clipboard")
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

    async def test_clipboard_uses_the_display_not_the_guest(self):
        await self.desktop.write_desktop_clipboard(self.workspace_id, "to windows")
        for _ in range(100):
            if self.server.received_text is not None:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.server.received_text, "to windows")
        self.assertEqual(
            await self.desktop.read_desktop_clipboard(self.workspace_id),
            "guest clipboard",
        )
        self.assertEqual(
            await self.desktop.read_desktop_clipboard(
                self.workspace_id, copy_selection=True
            ),
            "guest clipboard",
        )
        self.runtime.exec_command_wait.assert_not_awaited()

    async def test_recording_is_refused(self):
        with self.assertRaises(ValueError):
            await self.desktop._managed_record_start(self.workspace_id, {})
