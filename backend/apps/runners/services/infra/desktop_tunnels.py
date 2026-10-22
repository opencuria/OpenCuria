"""In-process registry of live browser<->runner desktop WebSocket tunnels.

The desktop proxy registers a queue per browser WebSocket; the Socket.IO
adapter pushes runner-emitted frames into it. Both adapters depend on this
module only, never on each other. Frames are accepted only from the runner
that owns the tunnel.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass


@dataclass
class _RunnerWebSocketTunnel:
    """Backend-side state for a browser<->runner desktop tunnel."""

    workspace_id: str
    runner_id: str
    queue: asyncio.Queue[dict]


_WS_TUNNELS: dict[str, _RunnerWebSocketTunnel] = {}


def register_ws_tunnel(
    tunnel_id: str,
    workspace_id: str,
    runner_id: str,
) -> asyncio.Queue[dict]:
    """Register a backend-side queue for a live desktop WebSocket tunnel."""
    queue: asyncio.Queue[dict] = asyncio.Queue()
    _WS_TUNNELS[tunnel_id] = _RunnerWebSocketTunnel(
        workspace_id=workspace_id,
        runner_id=runner_id,
        queue=queue,
    )
    return queue


def unregister_ws_tunnel(tunnel_id: str) -> None:
    """Drop backend-side state for a desktop WebSocket tunnel."""
    _WS_TUNNELS.pop(tunnel_id, None)


async def push_runner_ws_frame(
    tunnel_id: str,
    runner_id: str,
    *,
    text: str | None = None,
    data: str | None = None,
    encoding: str | None = None,
) -> None:
    """Deliver a runner-emitted desktop frame into the browser-facing queue."""
    tunnel = _WS_TUNNELS.get(tunnel_id)
    if tunnel is None or tunnel.runner_id != runner_id:
        return

    if text is not None:
        await tunnel.queue.put({"type": "text", "data": text})
        return

    if data is None:
        return

    payload = base64.b64decode(data) if encoding == "base64" else data.encode()
    await tunnel.queue.put({"type": "binary", "data": payload})


async def push_runner_ws_closed(
    tunnel_id: str,
    runner_id: str,
    *,
    code: int = 1000,
) -> None:
    """Notify the browser-facing side that the runner tunnel closed."""
    tunnel = _WS_TUNNELS.get(tunnel_id)
    if tunnel is None or tunnel.runner_id != runner_id:
        return
    await tunnel.queue.put({"type": "close", "code": code})
