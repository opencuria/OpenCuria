"""Independent durable delivery worker; Redis is transport only."""

import asyncio
import os

import socketio
import structlog
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from redis.asyncio import Redis

from apps.runners.services.recovery import RecoveryService
from common.systemd import notify_systemd

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Recover durable runner lifecycle commands (run alongside ASGI)"

    def handle(self, *args, **options) -> None:
        """Run the independent recovery loop until shutdown."""
        asyncio.run(self.run())

    async def run(self) -> None:
        """Isolate whole-tick failures and back off without abandoning intents."""
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        manager = socketio.AsyncRedisManager(
            redis_url,
            channel="opencuria-runner",
            write_only=True,
        )
        service = RecoveryService()
        failures = 0
        ready = False
        logger.info("lifecycle_worker_started")
        async with Redis.from_url(
            redis_url, socket_connect_timeout=5, socket_timeout=5
        ) as broker:
            while True:
                try:
                    # Redis delivery must work even when there are no due rows.
                    await broker.ping()
                    await sync_to_async(close_old_connections)()
                    await service.tick(manager)
                    failures = 0
                    notification = (
                        "WATCHDOG=1\nSTATUS=Lifecycle recovery tick succeeded"
                    )
                    if not ready:
                        notification = "READY=1\n" + notification
                        ready = True
                        logger.info("lifecycle_worker_ready")
                    notify_systemd(notification)
                except Exception:
                    failures += 1
                    logger.exception("lifecycle_worker_tick_failed", failures=failures)
                    # Failures do not renew the watchdog: a stuck delivery loop must
                    # be restarted even when its Python process is still alive.
                    notify_systemd(
                        f"STATUS=Lifecycle recovery tick failed ({failures})"
                    )
                await asyncio.sleep(min(60, 2 ** min(failures + 1, 6)))
