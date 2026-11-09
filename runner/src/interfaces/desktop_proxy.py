"""Runner-local desktop HTTP and WebSocket forwarding and tunnel ownership.

The control interface supplies desktop lookup, capture admission and an event
sink. This component owns upstream connections and their readers; it never
imports the runner control adapter or workspace orchestration.

The guest is untrusted: every upstream connection is pinned to the guest's
KasmVNC listener and redirects are refused, so a workspace can never make the
runner host dial another VM or a host-local service.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
import structlog

from ..models import DesktopSession
from ..services.capture_fence import CaptureFence

logger = structlog.get_logger(__name__)

#: Guest port of KasmVNC's web client and WebSocket listener.
DESKTOP_UPSTREAM_PORT = 6901
#: The largest KasmVNC client file is below 1 MiB.
_MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "transfer-encoding",
        "connection",
        "keep-alive",
        # aiohttp decompresses the payload before it is read. The browser must
        # receive headers describing those bytes.
        "content-encoding",
        "content-length",
    }
)


class DesktopUpstreamError(aiohttp.ClientError):
    """The guest answered outside the pinned desktop upstream contract."""


def _pinned_upstream(guest_ip: str, port: int) -> aiohttp.ClientMiddlewareType:
    """Return a middleware that allows only the guest listener and no redirects.

    aiohttp runs session middlewares for every hop, including redirect hops
    and WebSocket handshakes, which have no ``allow_redirects`` switch.
    """

    async def middleware(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        url = request.url
        if url.scheme not in ("http", "ws") or (url.host, url.port) != (
            guest_ip,
            port,
        ):
            raise DesktopUpstreamError("Desktop upstream outside the guest listener")
        response = await handler(request)
        if 300 <= response.status < 400:
            response.release()
            raise DesktopUpstreamError("Desktop upstream redirects are refused")
        return response

    return middleware


def _is_plain_path(rest_path: str) -> bool:
    """Return whether *rest_path* is an absolute path without query or fragment."""
    return (
        rest_path.startswith("/")
        and rest_path.isascii()
        and rest_path.isprintable()
        and not any(char in rest_path for char in "?#\\")
        and ".." not in rest_path.split("/")
    )


def _bad_gateway() -> dict[str, object]:
    """Describe an unusable upstream answer without forwarding any of it."""
    return {
        "status": 502,
        "headers": [["Content-Type", "text/plain"]],
        "body": base64.b64encode(b"Bad Gateway").decode("ascii"),
        "body_encoding": "base64",
    }


async def _read_limited(response: aiohttp.ClientResponse) -> bytes | None:
    """Read the decompressed body, or return None once it exceeds the limit."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > _MAX_HTTP_BODY_BYTES:
            return None
    return bytes(body)


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


@dataclass
class _OpeningTunnel:
    """Handshake resources that must also obey desktop/transport teardown."""

    workspace_id: uuid.UUID
    desktop_generation: int | None
    session: aiohttp.ClientSession | None = None
    invalidated: bool = False


class DesktopProxy:
    """Forward desktop traffic and own all upstream connection resources."""

    def __init__(
        self,
        get_guest_ip: Callable[[uuid.UUID], str],
        get_session: Callable[[uuid.UUID], DesktopSession | None],
        events: DesktopProxyEvents,
        capture_fence: CaptureFence | None = None,
        *,
        upstream_port: int = DESKTOP_UPSTREAM_PORT,
    ) -> None:
        """Inject desktop observations, outbound transport and capture admission."""
        self._get_guest_ip = get_guest_ip
        self._get_session = get_session
        self._events = events
        self._capture_fence = capture_fence
        self._port = upstream_port
        self._tunnels: dict[str, _Tunnel] = {}
        self._opening: dict[str, _OpeningTunnel] = {}

    def _client_session(self, guest_ip: str, **options: Any) -> aiohttp.ClientSession:
        """Open a session that can reach only this guest's desktop listener."""
        return aiohttp.ClientSession(
            middlewares=(_pinned_upstream(guest_ip, self._port),), **options
        )

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
    ) -> dict[str, object]:
        """Fetch one desktop client file from the local workspace runtime.

        Query strings are never forwarded to the guest. Redirects, oversized
        bodies and connection failures yield a 502 answer instead of raising.
        """
        if not _is_plain_path(rest_path):
            return _bad_gateway()
        async with self._interaction(workspace_id):
            guest_ip = self._get_guest_ip(workspace_id)
            upstream_url = f"http://{guest_ip}:{self._port}{rest_path}"
            try:
                async with (
                    self._client_session(guest_ip, timeout=_HTTP_TIMEOUT) as session,
                    session.get(upstream_url, allow_redirects=False) as resp,
                ):
                    body = await _read_limited(resp)
                    if body is None:
                        logger.warning(
                            "desktop_proxy_http_body_too_large",
                            workspace_id=str(workspace_id),
                        )
                        return _bad_gateway()
                    headers = [
                        [key, value]
                        for key, value in resp.headers.items()
                        if key.lower() not in _HOP_BY_HOP_HEADERS
                    ]
                    headers.append(["Content-Length", str(len(body))])
                    return {
                        "status": resp.status,
                        "headers": headers,
                        "body": base64.b64encode(body).decode("ascii"),
                        "body_encoding": "base64",
                    }
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                logger.warning(
                    "desktop_proxy_http_failed",
                    workspace_id=str(workspace_id),
                    error_type=type(exc).__name__,
                )
                return _bad_gateway()

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
        if tunnel_id in self._tunnels or tunnel_id in self._opening:
            raise ValueError("Desktop proxy tunnel already exists")
        desktop_session = self._get_session(workspace_id)
        generation = desktop_session.generation if desktop_session else None
        # Register before admission's first await: a timeout/transport close
        # must also fence requests queued behind the workspace lifecycle lock.
        opening = _OpeningTunnel(workspace_id, generation)
        self._opening[tunnel_id] = opening
        try:
            async with self._interaction(workspace_id):
                current = self._get_session(workspace_id)
                current_generation = current.generation if current else None
                if opening.invalidated or current_generation != generation:
                    raise RuntimeError("Desktop changed while opening proxy tunnel")
                guest_ip = self._get_guest_ip(workspace_id)
                upstream_url = f"ws://{guest_ip}:{self._port}/websockify"
                upstream_origin = f"http://{guest_ip}:{self._port}"
                chosen_protocol = "binary" if "binary" in (subprotocols or []) else None

                session = self._client_session(guest_ip)
                opening.session = session
                websocket = await session.ws_connect(
                    upstream_url,
                    protocols=["binary"] if chosen_protocol else None,
                    max_msg_size=16 * 1024 * 1024,
                    headers={"Origin": upstream_origin},
                )
                current = self._get_session(workspace_id)
                current_generation = current.generation if current else None
                if opening.invalidated or current_generation != generation:
                    await websocket.close()
                    raise RuntimeError("Desktop changed while opening proxy tunnel")

                reader_task = asyncio.create_task(self._read(tunnel_id, websocket))
                self._tunnels[tunnel_id] = _Tunnel(
                    session=session,
                    websocket=websocket,
                    reader_task=reader_task,
                    workspace_id=workspace_id,
                    desktop_generation=generation,
                )
                return {"ok": True, "subprotocol": chosen_protocol}
        except BaseException:
            # Cancellation is not an Exception on supported Python versions.
            if tunnel_id in self._tunnels:
                await self._finalize(tunnel_id)
            elif opening.session is not None:
                await opening.session.close()
            raise
        finally:
            if self._opening.get(tunnel_id) is opening:
                self._opening.pop(tunnel_id)

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
            for tunnel_id, tunnel in {**self._tunnels, **self._opening}.items()
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
                await self._close_opening(tunnel_id)
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
        await self._close_opening(tunnel_id)
        tunnel = self._tunnels.get(tunnel_id)
        if tunnel is None:
            return
        await tunnel.websocket.close()

    async def _close_opening(self, tunnel_id: str) -> None:
        """Fence a pending handshake before closing its HTTP connection."""
        opening = self._opening.get(tunnel_id)
        if opening is not None:
            opening.invalidated = True
            if opening.session is not None:
                await opening.session.close()

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
        for tunnel_id in [*self._opening, *self._tunnels]:
            with contextlib.suppress(Exception):
                await self.close_tunnel(tunnel_id)
