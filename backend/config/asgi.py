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


def _scheduler_enabled() -> bool:
    """Preserve legacy ASGI scheduling but disable it for managed web gateways."""
    configured = os.environ.get("OPENCURIA_SCHEDULER_ENABLED")
    if configured is None:
        return os.environ.get("OPENCURIA_MANAGED_DEPLOYMENT", "0") != "1"
    return configured.strip().lower() in {"1", "true", "yes", "on"}


async def application(scope, receive, send):
    """Route requests and optionally run the legacy single-process scheduler."""
    if scope["type"] == "lifespan":
        scheduler = None
        if _scheduler_enabled():
            from apps.scheduled_tasks.services import ScheduledTaskScheduler

            scheduler = ScheduledTaskScheduler()
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if os.environ.get("OPENCURIA_HARNESS_MODE") == "gateway":
                    from apps.harness.runner_rpc import get_gateway_broker
                    from apps.runners.sio_server import (
                        get_runner_service,
                        get_sio_server,
                    )

                    service = get_runner_service()
                    await get_gateway_broker().start(get_sio_server())
                    client = getattr(service, "_harness_runner_rpc_client", None)
                    if client is not None:
                        await client.start()
                if scheduler is not None:
                    try:
                        await scheduler.start()
                    except Exception as exc:
                        await send(
                            {"type": "lifespan.startup.failed", "message": str(exc)}
                        )
                        return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if scheduler is not None:
                    await scheduler.stop()
                if os.environ.get("OPENCURIA_HARNESS_MODE") == "gateway":
                    from apps.harness.runner_rpc import get_gateway_broker

                    await get_gateway_broker().close()
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
