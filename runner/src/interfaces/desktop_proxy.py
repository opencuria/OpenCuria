"""Runner-local desktop HTTP and WebSocket forwarding and tunnel ownership.

The control interface supplies desktop lookup, capture admission and an event
sink. This component owns upstream connections and their readers; it never
imports the runner control adapter or workspace orchestration.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

import aiohttp
import structlog

from ..models import DesktopSession
from ..services.capture_fence import CaptureFence

logger = structlog.get_logger(__name__)


class DesktopProxyEvents(Protocol):
    """Connection state and outbound events supplied by the control transport."""

    @property
    def connected(self) -> bool:
        """Whether tunnel closure can be reported to the backend."""
        ...

    async def emit(self, event: str, data: dict) -> None:
        """Forward a frame or closure to the backend."""
        ...


@dataclass
class _Tunnel:
    """Resources owned by one desktop WebSocket tunnel."""

    session: aiohttp.ClientSession
    websocket: aiohttp.ClientWebSocketResponse
    reader_task: asyncio.Task[None]
    workspace_id: uuid.UUID
    desktop_generation: int | None = None


class DesktopProxy:
    """Forward desktop traffic and own all upstream connection resources."""

    def __init__(
        self,
        get_guest_ip: Callable[[uuid.UUID], str],
        get_session: Callable[[uuid.UUID], DesktopSession | None],
        events: DesktopProxyEvents,
        capture_fence: CaptureFence | None = None,
    ) -> None:
        """Inject desktop observations, outbound transport and capture admission."""
        self._get_guest_ip = get_guest_ip
        self._get_session = get_session
        self._events = events
        self._capture_fence = capture_fence
        self._tunnels: dict[str, _Tunnel] = {}

    def _interaction(
        self, workspace_id: uuid.UUID | None
    ) -> AbstractAsyncContextManager[None]:
        if self._capture_fence is None or workspace_id is None:
            return contextlib.nullcontext()
        return self._capture_fence.interaction(workspace_id)

    async def fetch_http(
        self,
        workspace_id: uuid.UUID,
        rest_path: str,
        query_string: str = "",
    ) -> dict[str, object]:
        """Fetch a desktop HTTP resource from the local workspace runtime."""
        async with self._interaction(workspace_id):
            guest_ip = self._get_guest_ip(workspace_id)
            upstream_url = f"http://{guest_ip}:6901{rest_path}"
            if query_string:
                upstream_url = f"{upstream_url}?{query_string}"

            async with (
                aiohttp.ClientSession() as session,
                session.get(upstream_url) as resp,
            ):
                headers: list[list[str]] = []
                for key, value in resp.headers.items():
                    if key.lower() in ("transfer-encoding", "connection", "keep-alive"):
                        continue
                    headers.append([key, value])

                body = await resp.read()
                return {
                    "status": resp.status,
                    "headers": headers,
                    "body": base64.b64encode(body).decode("ascii"),
                    "body_encoding": "base64",
                }

    async def _read(
        self,
        tunnel_id: str,
        websocket: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """Forward upstream desktop WebSocket frames back to the backend."""
        close_code = 1000
        try:
            async for msg in websocket:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._events.emit(
                        "desktop:proxy_ws_frame",
                        {
                            "tunnel_id": tunnel_id,
                            "data": base64.b64encode(msg.data).decode("ascii"),
                            "encoding": "base64",
                        },
                    )
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await self._events.emit(
                        "desktop:proxy_ws_frame",
                        {
                            "tunnel_id": tunnel_id,
                            "text": msg.data,
                        },
                    )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    close_code = websocket.close_code or 1000
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    close_code = 1011
                    break
        except asyncio.CancelledError:
            close_code = websocket.close_code or 1000
            raise
        except Exception:
            logger.exception("desktop_proxy_reader_failed", tunnel_id=tunnel_id)
            close_code = 1011
        finally:
            await self._finalize(tunnel_id, close_code=close_code)

    async def open_tunnel(
        self,
        workspace_id: uuid.UUID,
        tunnel_id: str,
        subprotocols: list[str] | None = None,
    ) -> dict[str, object]:
        """Open a runner-local WebSocket tunnel to the desktop session."""
        async with self._interaction(workspace_id):
            guest_ip = self._get_guest_ip(workspace_id)
            upstream_url = f"ws://{guest_ip}:6901/websockify"
            upstream_origin = f"http://{guest_ip}:6901"
            chosen_protocol = "binary" if "binary" in (subprotocols or []) else None

            session = aiohttp.ClientSession()
            try:
                websocket = await session.ws_connect(
                    upstream_url,
                    protocols=["binary"] if chosen_protocol else None,
                    max_msg_size=16 * 1024 * 1024,
                    headers={"Origin": upstream_origin},
                )
            except Exception:
                await session.close()
                raise

            reader_task = asyncio.create_task(self._read(tunnel_id, websocket))
            desktop_session = self._get_session(workspace_id)
            self._tunnels[tunnel_id] = _Tunnel(
                session=session,
                websocket=websocket,
                reader_task=reader_task,
                workspace_id=workspace_id,
                desktop_generation=(
                    desktop_session.generation if desktop_session is not None else None
                ),
            )
            return {"ok": True, "subprotocol": chosen_protocol}

    async def close_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        generation: int | None = None,
        keep_generation: int | None = None,
        include_generation: bool = False,
    ) -> None:
        """Close runner-side desktop proxy tunnels for one workspace.

        Called after a desktop stop/restart so stale browser WebSockets can
        never stay attached to a recycled Xvnc process. Only tunnels of
        *workspace_id* are touched; other workspaces keep their tunnels.
        """
        tunnel_ids = [
            tunnel_id
            for tunnel_id, tunnel in self._tunnels.items()
            if tunnel.workspace_id == workspace_id
            and (
                generation is None
                or tunnel.desktop_generation is None
                or tunnel.desktop_generation < generation
                or include_generation
                and tunnel.desktop_generation == generation
            )
            and (
                keep_generation is None or tunnel.desktop_generation != keep_generation
            )
        ]
        for tunnel_id in tunnel_ids:
            with contextlib.suppress(Exception):
                await self._finalize(tunnel_id)

    async def send(
        self,
        tunnel_id: str,
        *,
        text: str | None = None,
        data: str | None = None,
        encoding: str | None = None,
    ) -> None:
        """Forward a browser frame from the backend to the upstream desktop WS."""
        workspace_id = getattr(self._tunnels.get(tunnel_id), "workspace_id", None)
        async with self._interaction(workspace_id):
            tunnel = self._tunnels.get(tunnel_id)
            if tunnel is None:
                raise RuntimeError(f"Unknown desktop proxy tunnel: {tunnel_id}")

            if text is not None:
                await tunnel.websocket.send_str(text)
                return

            if data is None:
                return

            if encoding == "base64":
                await tunnel.websocket.send_bytes(base64.b64decode(data))
                return

            await tunnel.websocket.send_bytes(data.encode("utf-8"))

    async def close_tunnel(self, tunnel_id: str) -> None:
        """Close an active desktop WebSocket tunnel."""
        tunnel = self._tunnels.get(tunnel_id)
        if tunnel is None:
            return
        await tunnel.websocket.close()

    async def _finalize(
        self,
        tunnel_id: str,
        *,
        close_code: int = 1000,
    ) -> None:
        """Release runner-side resources for a desktop proxy tunnel."""
        tunnel = self._tunnels.pop(tunnel_id, None)
        if tunnel is None:
            return

        current = asyncio.current_task()
        if tunnel.reader_task is not current:
            tunnel.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tunnel.reader_task

        if not tunnel.websocket.closed:
            with contextlib.suppress(Exception):
                await tunnel.websocket.close()
        with contextlib.suppress(Exception):
            await tunnel.session.close()

        if self._events.connected:
            await self._events.emit(
                "desktop:proxy_ws_closed",
                {
                    "tunnel_id": tunnel_id,
                    "code": close_code,
                },
            )

    async def close_all(self) -> None:
        """Request closure of every tunnel when the control connection drops."""
        for tunnel_id in list(self._tunnels):
            with contextlib.suppress(Exception):
                await self.close_tunnel(tunnel_id)
