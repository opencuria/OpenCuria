"""Minimal RFB client for QEMU's display (Windows guests).

Windows guests have no desktop server inside the VM: QEMU's built-in VNC server
shows the console. The runner reaches it only through a libvirt-provided socket
(``virDomainOpenGraphicsFD``), never over TCP. This client covers what the
runner itself needs besides forwarding viewer traffic: a liveness handshake and
the clipboard through the extended clipboard pseudo-encoding, which QEMU bridges
to the guest via ``qemu-vdagent`` and the SPICE agent in Windows.
"""

from __future__ import annotations

import asyncio
import struct
import zlib

_VERSION = b"RFB 003.008\n"
_SECURITY_NONE = 1
_EXTENDED_CLIPBOARD = -1063131698  # 0xC0A1E5CE as a signed 32-bit encoding
_RAW = 0

# Extended clipboard flags: formats in the low byte, actions in the high byte.
_TEXT = 1 << 0
_CAPS = 1 << 24
_REQUEST = 1 << 25
_NOTIFY = 1 << 27
_PROVIDE = 1 << 28

_KEY_CONTROL = 0xFFE3
_KEY_C = 0x63

#: Largest clipboard text the runner exchanges (matches the desktop clipboard API).
MAX_CLIPBOARD_BYTES = 1024 * 1024


class RFBError(RuntimeError):
    """The display did not speak the expected RFB protocol."""


class RFBClient:
    """One shared, view-only control connection to a QEMU VNC server."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.width = 0
        self.height = 0
        self.name = ""

    async def handshake(self) -> None:
        """Negotiate RFB 3.8 without authentication and request clipboard support."""
        banner = await self._reader.readexactly(12)
        if not banner.startswith(b"RFB 003."):
            raise RFBError("Display did not send an RFB banner")
        self._writer.write(_VERSION)
        count = (await self._reader.readexactly(1))[0]
        if count == 0:
            length = struct.unpack(">I", await self._reader.readexactly(4))[0]
            await self._reader.readexactly(min(length, 4096))
            raise RFBError("Display refused the connection")
        types = await self._reader.readexactly(count)
        if _SECURITY_NONE not in types:
            raise RFBError("Display requires authentication")
        self._writer.write(bytes([_SECURITY_NONE]))
        if struct.unpack(">I", await self._reader.readexactly(4))[0] != 0:
            raise RFBError("Display rejected the connection")
        self._writer.write(b"\x01")  # shared: never disconnect viewers
        header = await self._reader.readexactly(24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        name_length = struct.unpack(">I", header[20:24])[0]
        self.name = (await self._reader.readexactly(min(name_length, 4096))).decode(
            "utf-8", "replace"
        )
        encodings = (_RAW, _EXTENDED_CLIPBOARD)
        self._writer.write(
            struct.pack(f">BxH{len(encodings)}i", 2, len(encodings), *encodings)
        )
        await self._writer.drain()

    async def write_clipboard(self, text: str) -> None:
        """Replace the guest clipboard with ``text``."""
        data = text.encode("utf-8")
        if len(data) > MAX_CLIPBOARD_BYTES:
            raise ValueError("Clipboard text is too large")
        payload = zlib.compress(struct.pack(">I", len(data) + 1) + data + b"\x00")
        self._send_cut_text(_PROVIDE | _TEXT, payload)
        await self._writer.drain()

    async def read_clipboard(self, timeout: float) -> str:
        """Return the guest clipboard text (empty if it holds no text)."""
        self._send_cut_text(_REQUEST | _TEXT, b"")
        await self._writer.drain()
        return await self._await_provided_text(timeout)

    async def copy_selection(self, timeout: float) -> str:
        """Press Ctrl+C in the guest and return the clipboard it produces."""
        for key, down in ((_KEY_CONTROL, 1), (_KEY_C, 1), (_KEY_C, 0), (_KEY_CONTROL, 0)):
            self._writer.write(struct.pack(">BBxxI", 4, down, key))
        await self._writer.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            flags, _ = await asyncio.wait_for(self._next_clipboard_message(), remaining)
            if flags & _NOTIFY and flags & _TEXT:
                return await self.read_clipboard(max(0.1, deadline - loop.time()))
            if flags & _PROVIDE:
                continue
        raise TimeoutError("The guest clipboard did not change")

    def _send_cut_text(self, flags: int, payload: bytes) -> None:
        body = struct.pack(">I", flags) + payload
        # A negative length marks an extended clipboard message.
        self._writer.write(struct.pack(">Bxxxi", 6, -len(body)) + body)

    async def _await_provided_text(self, timeout: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            flags, payload = await asyncio.wait_for(
                self._next_clipboard_message(), remaining
            )
            if flags & _PROVIDE:
                return _provided_text(flags, payload)
        raise TimeoutError("The guest clipboard did not answer")

    async def _next_clipboard_message(self) -> tuple[int, bytes]:
        """Skip unrelated server messages until an extended clipboard message."""
        while True:
            kind = (await self._reader.readexactly(1))[0]
            if kind == 2:  # Bell
                continue
            if kind != 3:
                raise RFBError(f"Unexpected server message {kind}")
            await self._reader.readexactly(3)
            length = struct.unpack(">i", await self._reader.readexactly(4))[0]
            if length >= 0:  # legacy Latin-1 cut text
                if length > MAX_CLIPBOARD_BYTES:
                    raise RFBError("Clipboard message too large")
                await self._reader.readexactly(length)
                continue
            length = -length
            if length < 4 or length > 2 * MAX_CLIPBOARD_BYTES:
                raise RFBError("Invalid extended clipboard message")
            body = await self._reader.readexactly(length)
            flags = struct.unpack(">I", body[:4])[0]
            if flags & _CAPS:
                continue
            return flags, body[4:]

    async def close(self) -> None:
        """Close the connection without waiting for the server."""
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError):
            pass


def _provided_text(flags: int, payload: bytes) -> str:
    """Decode the text format of an extended clipboard ``provide`` message."""
    if not flags & _TEXT or not payload:
        return ""
    data = zlib.decompressobj().decompress(payload, MAX_CLIPBOARD_BYTES + 8)
    offset = 0
    for bit in range(16):
        if not flags & (1 << bit):
            continue
        if offset + 4 > len(data):
            raise RFBError("Truncated clipboard data")
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        chunk = data[offset : offset + size]
        offset += size
        if bit == 0:
            return chunk.rstrip(b"\x00").decode("utf-8", "replace").replace("\r\n", "\n")
    return ""
