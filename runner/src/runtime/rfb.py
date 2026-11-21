"""Minimal RFB client for QEMU's display (Windows guests).

Windows guests have no desktop server inside the VM: QEMU's built-in VNC server
shows the console. The runner reaches it only through a libvirt-provided socket
(``virDomainOpenGraphicsFD``), never over TCP. Viewer traffic is forwarded as
raw bytes; the runner itself only needs this handshake to prove the display is
live. Clipboard and display size run inside the guest session instead.
"""

from __future__ import annotations

import asyncio
import struct

_VERSION = b"RFB 003.008\n"
_SECURITY_NONE = 1


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
        """Negotiate RFB 3.8 without authentication as a shared client."""
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

    async def close(self) -> None:
        """Close the connection without waiting for the server."""
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError):
            pass
