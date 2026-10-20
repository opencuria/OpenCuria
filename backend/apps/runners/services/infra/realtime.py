"""Socket.IO transport infrastructure shared by services and adapters.

Owns the process-wide python-socketio ``AsyncServer`` and the in-process
registry of frontend workspace subscriptions, plus ``emit_to_frontend``
which fans events out to subscribed frontend clients.

Event handlers are *not* registered here: the Socket.IO adapter
(``apps.runners.sio_server``) attaches them when it builds the ASGI app.
Services depend on this module only, never on the adapter.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict

import socketio

logger = logging.getLogger(__name__)

# The Socket.IO server — created once, shared across the app
_sio: socketio.AsyncServer | None = None

# Frontend subscription tracking: workspace_id -> set of frontend sids
_frontend_subscriptions: dict[str, set[str]] = defaultdict(set)
# Reverse mapping: frontend sid -> set of workspace_ids
_frontend_sid_workspaces: dict[str, set[str]] = defaultdict(set)


def get_sio_server() -> socketio.AsyncServer:
    """Get or create the Socket.IO server singleton (without handlers)."""
    global _sio
    if _sio is None:
        from django.conf import settings as django_settings

        cors_origins = getattr(django_settings, "SIO_CORS_ALLOWED_ORIGINS", "*")
        _sio = socketio.AsyncServer(
            async_mode="asgi",
            client_manager=socketio.AsyncRedisManager(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                channel="opencuria-runner",
            ),
            cors_allowed_origins=cors_origins,
            logger=False,
            engineio_logger=False,
            # Keep the 200 MiB engine.io HTTP buffer for monolithic control
            # traffic (computer-use PNG screenshots, video). Chunking keeps
            # file transfers under Daphne's ~1 MiB default, but screenshots
            # are still a single WebSocket frame. Align
            # DAPHNE_WEBSOCKET_MAX_MESSAGE/FRAME_SIZE (settings) and the
            # daphne CLI flags in entrypoint.sh at the same 200 MiB value.
            max_http_buffer_size=200 * 1024 * 1024,
            # The runner answers server->client events with Socket.IO
            # ACKs (``sio.call``); without an active Engine.IO monitor
            # task, a stalled socket is never pinged/closed and ACKs can
            # hang until the caller's timeout. Keep the monitor enabled
            # explicitly so control calls fail fast instead of hanging.
            monitor_clients=True,
            ping_interval=25,
            ping_timeout=20,
        )
    return _sio


def subscribe_frontend(sid: str, workspace_id: str) -> None:
    """Subscribe frontend client *sid* to events of *workspace_id*."""
    _frontend_subscriptions[workspace_id].add(sid)
    _frontend_sid_workspaces[sid].add(workspace_id)


def unsubscribe_frontend(sid: str, workspace_id: str) -> None:
    """Unsubscribe frontend client *sid* from events of *workspace_id*."""
    _frontend_subscriptions[workspace_id].discard(sid)
    if not _frontend_subscriptions[workspace_id]:
        del _frontend_subscriptions[workspace_id]
    _frontend_sid_workspaces[sid].discard(workspace_id)


def drop_frontend(sid: str) -> None:
    """Remove every workspace subscription of a disconnected client."""
    workspace_ids = _frontend_sid_workspaces.pop(sid, set())
    for ws_id in workspace_ids:
        _frontend_subscriptions[ws_id].discard(sid)
        if not _frontend_subscriptions[ws_id]:
            del _frontend_subscriptions[ws_id]


async def emit_to_frontend(
    event: str,
    data: dict,
    workspace_id: str,
) -> None:
    """
    Emit an event to all frontend clients subscribed to a workspace.

    This is called by the RunnerService when processing runner events
    to forward real-time updates to the frontend.
    """
    sio = get_sio_server()
    sids = _frontend_subscriptions.get(workspace_id, set())
    if not sids:
        return

    for sid in sids.copy():
        try:
            await sio.emit(event, data, to=sid, namespace="/frontend")
        except Exception:
            logger.warning("Failed to emit %s to frontend sid %s", event, sid)
