"""Independent durable delivery worker; Redis is transport only."""

import asyncio
import os

import socketio
import structlog
from django.core.management.base import BaseCommand

from apps.runners.services.recovery import RecoveryService


class Command(BaseCommand):
    help = "Recover durable runner lifecycle commands (run alongside ASGI)"

    def handle(self, *args, **options) -> None:
        """Run the independent recovery loop until shutdown."""
        asyncio.run(self.run())

    async def run(self) -> None:
        """Isolate whole-tick failures and back off without abandoning intents."""
        manager = socketio.AsyncRedisManager(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            channel="opencuria-runner",
            write_only=True,
        )
        service = RecoveryService()
        failures = 0
        while True:
            try:
                await service.tick(manager)
                failures = 0
            except Exception:
                failures += 1
                structlog.get_logger(__name__).exception("lifecycle_worker_tick_failed")
            await asyncio.sleep(min(60, 2 ** min(failures + 1, 6)))
