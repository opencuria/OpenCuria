"""
ASGI config for opencuria backend.

Combines Django HTTP, Socket.IO for runners and frontend clients,
desktop WebSocket streams and the gateway from published apps to the
processes of their workspace.
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

from apps.mobile_apps.gateway import app_backend_gateway  # noqa: E402
from apps.mobile_apps.gateway_protocol import GATEWAY_PREFIX  # noqa: E402
from apps.runners.desktop_proxy import desktop_proxy_app  # noqa: E402
from apps.runners.sio_server import create_sio_app  # noqa: E402

django_asgi = get_asgi_application()
sio_asgi = create_sio_app()


async def application(scope, receive, send):
    """Route API traffic; only transport infrastructure shares this lifetime."""
    if scope["type"] == "lifespan":
        from apps.runners.reply_routing import (
            start_runner_reply_relay,
            stop_runner_reply_relay,
        )

        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await start_runner_reply_relay()
                except Exception as exc:
                    await stop_runner_reply_relay()
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await stop_runner_reply_relay()
                await send({"type": "lifespan.shutdown.complete"})
                return
        return
    path = scope.get("path", "")

    if path.startswith("/ws/desktop/"):
        await desktop_proxy_app(scope, receive, send)
    elif path.startswith(f"{GATEWAY_PREFIX}/"):
        await app_backend_gateway(scope, receive, send)
    elif (
        path.startswith("/ws/runner")
        or path.startswith("/ws/frontend")
        or path.startswith("/socket.io")
    ):
        await sio_asgi(scope, receive, send)
    else:
        await django_asgi(scope, receive, send)
