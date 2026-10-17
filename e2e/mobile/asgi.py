"""Private ASGI entrypoint; install process-local fixture Socket.IO first."""

from __future__ import annotations

import importlib
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "e2e.mobile.settings")

# Importing this module initializes Django and swaps in the in-process
# Socket.IO manager before config.asgi creates the ordinary ASGI multiplexer.
importlib.import_module("e2e.mobile.fixture")
_production_application = importlib.import_module("config.asgi").application


async def application(scope, receive, send):
    """Run real ASGI protocol, omitting only the unrelated global scheduler."""
    if scope["type"] != "lifespan":
        await _production_application(scope, receive, send)
        return

    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
