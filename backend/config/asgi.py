"""
ASGI config for opencuria backend.

Combines Django HTTP, Django Channels WebSocket, Socket.IO for runners,
and the MCP (Model Context Protocol) SSE server.
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

from apps.mcp_app.server import get_mcp_app  # noqa: E402
from apps.runners.desktop_proxy import desktop_proxy_app  # noqa: E402
from apps.runners.sio_server import create_sio_app  # noqa: E402

django_asgi = get_asgi_application()
sio_asgi = create_sio_app()
mcp_asgi = get_mcp_app()


async def application(scope, receive, send):
    """Route requests and run the scheduler in the ASGI server's event loop."""
    if scope["type"] == "lifespan":
        from apps.scheduled_tasks.services import ScheduledTaskScheduler

        scheduler = ScheduledTaskScheduler()
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await scheduler.start()
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await scheduler.stop()
                await send({"type": "lifespan.shutdown.complete"})
                return
        return
    path = scope.get("path", "")

    if path.startswith("/ws/desktop/"):
        await desktop_proxy_app(scope, receive, send)
    elif (
        path.startswith("/ws/runner")
        or path.startswith("/ws/frontend")
        or path.startswith("/socket.io")
    ):
        await sio_asgi(scope, receive, send)
    elif path == "/mcp" or path.startswith("/mcp/"):
        await mcp_asgi(scope, receive, send)
    else:
        await django_asgi(scope, receive, send)
