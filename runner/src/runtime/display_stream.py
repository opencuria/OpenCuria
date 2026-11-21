"""Desktop upstream for guests whose display QEMU serves (Windows).

The viewer speaks RFB over a WebSocket; the runner forwards it to a libvirt
display socket instead of an in-guest websockify. The pinned noVNC web client
is served from the runner's own asset (the guest has no web server), and the
backend still verifies every byte against its manifest.
"""

from __future__ import annotations

import asyncio
import hashlib
import tarfile
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp

NOVNC_VERSION = "1.7.0"
_NOVNC_ARCHIVE = (
    Path(__file__).resolve().parent / "assets" / "novnc-1.7.0.tar.gz"
)
_NOVNC_SHA256 = "b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6"
_NOVNC_PREFIX = f"noVNC-{NOVNC_VERSION}/"
_CHUNK = 64 * 1024


@cache
def _novnc_files() -> dict[str, bytes]:
    """Return the runtime files of the pinned noVNC release, verified once."""
    data = _NOVNC_ARCHIVE.read_bytes()
    if hashlib.sha256(data).hexdigest() != _NOVNC_SHA256:
        raise RuntimeError("Pinned noVNC client archive is damaged")
    files: dict[str, bytes] = {}
    with tarfile.open(_NOVNC_ARCHIVE, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith(_NOVNC_PREFIX):
                continue
            relative = member.name[len(_NOVNC_PREFIX):]
            stream = archive.extractfile(member)
            if stream is not None:
                files[relative] = stream.read()
    return files


def novnc_file(rest_path: str) -> bytes | None:
    """Return one noVNC file for a proxied path such as ``/vnc.html``."""
    relative = PurePosixPath(rest_path.removeprefix("/")).as_posix()
    if relative.startswith("..") or "/../" in f"/{relative}/":
        return None
    return _novnc_files().get(relative)


class _Message:
    """The subset of ``aiohttp.WSMessage`` the desktop proxy reads."""

    __slots__ = ("type", "data")

    def __init__(self, type_: aiohttp.WSMsgType, data: Any) -> None:
        self.type = type_
        self.data = data


class DisplayStream:
    """Present a raw RFB byte stream like the WebSocket the proxy forwards.

    RFB carries no message framing of its own, so any chunking of the byte
    stream is valid on both legs.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.close_code: int | None = None

    @property
    def closed(self) -> bool:
        return self._writer.is_closing()

    def __aiter__(self) -> DisplayStream:
        return self

    async def __anext__(self) -> _Message:
        if self.closed:
            raise StopAsyncIteration
        try:
            chunk = await self._reader.read(_CHUNK)
        except (OSError, asyncio.IncompleteReadError):
            chunk = b""
        if not chunk:
            self.close_code = 1000
            return _Message(aiohttp.WSMsgType.CLOSED, None)
        return _Message(aiohttp.WSMsgType.BINARY, chunk)

    async def send_bytes(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def send_str(self, text: str) -> None:
        # RFB is binary; a text frame is a client error and closes the stream.
        del text
        await self.close()

    async def close(self) -> bool:
        if self.close_code is None:
            self.close_code = 1000
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError):
            pass
        return True
