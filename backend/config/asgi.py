"""
ASGI config for opencuria backend.

Combines Django HTTP, Socket.IO for runners and frontend clients,
and desktop WebSocket streams.
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

from apps.runners.desktop_proxy import desktop_proxy_app  # noqa: E402
from apps.runners.sio_server import create_sio_app  # noqa: E402

django_asgi = get_asgi_application()
sio_asgi = create_sio_app()


async def application(scope, receive, send):
    """Route requests and run the scheduler in the ASGI server's event loop."""
    if scope["type"] == "lifespan":
        from apps.harness.recovery_worker import HarnessRecoveryWorker
        from apps.notifications.worker import NotificationWorker
        from apps.scheduled_tasks.services import ScheduledTaskScheduler
        from common.process_ownership import acquire_backend_process_guard

        scheduler = ScheduledTaskScheduler()
        notifications = NotificationWorker()
        harness_recovery = HarnessRecoveryWorker()
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    acquire_backend_process_guard()
                    await scheduler.start(before_dispatch=harness_recovery.start)
                    await notifications.start()
                except Exception as exc:
                    await notifications.stop()
                    await harness_recovery.stop()
                    await scheduler.stop()
                    # A scheduled native turn may already have spawned while
                    # startup failed. Keep process ownership until process exit.
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await notifications.stop()
                await harness_recovery.stop()
                await scheduler.stop()
                # The guard deliberately survives lifespan shutdown. Native chat
                # tasks can still be draining until the ASGI process exits.
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
    else:
        await django_asgi(scope, receive, send)
