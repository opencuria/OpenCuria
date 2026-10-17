"""Independent durable lifecycle dispatcher with an exclusive PG lease."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress

import socketio
import structlog
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from redis.asyncio import Redis

from apps.runners.services import RunnerService
from apps.runners.services.recovery import RecoveryService
from common.systemd import notify_systemd

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Run the single lifecycle dispatcher until graceful process shutdown."""

    help = "Recover durable runner lifecycle commands (single leased process)"

    def add_arguments(self, parser):
        parser.add_argument("--owner-id", default="local")
        parser.add_argument("--interval", type=float, default=2.0)

    def handle(self, *args, **options) -> None:
        if options["interval"] < 0.1 or options["interval"] > 60:
            raise CommandError("--interval must be in 0.1..60 seconds")
        try:
            asyncio.run(self.run(options["owner_id"], options["interval"]))
        except KeyboardInterrupt:
            self.stdout.write("Recovery dispatcher stopped.")

    async def run(self, owner_id: str = "local", interval: float = 2.0) -> None:
        """Hold one stable advisory lock and deliver independent recovery ticks."""
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        manager = socketio.AsyncRedisManager(
            redis_url,
            channel="opencuria-runner",
            write_only=True,
        )
        recovery = RecoveryService(owner_id)
        await recovery.start()
        from apps.runners.sio_server import get_sio_server

        service = RunnerService(sio_server=get_sio_server())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop.set)
        failures = 0
        ready = False
        logger.info("lifecycle_worker_started", owner_id=owner_id)
        try:
            async with Redis.from_url(
                redis_url, socket_connect_timeout=5, socket_timeout=5
            ) as broker:
                while not stop.is_set():
                    try:
                        await broker.ping()
                        await sync_to_async(close_old_connections)()
                        await recovery.tick(manager, service=service)
                        failures = 0
                        notification = (
                            "WATCHDOG=1\nSTATUS=Lifecycle recovery tick succeeded"
                        )
                        if not ready:
                            notification = "READY=1\n" + notification
                            ready = True
                            logger.info("lifecycle_worker_ready", owner_id=owner_id)
                        notify_systemd(notification)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failures += 1
                        logger.exception(
                            "lifecycle_worker_tick_failed",
                            owner_id=owner_id,
                            failures=failures,
                        )
                        notify_systemd(
                            f"STATUS=Lifecycle recovery tick failed ({failures})"
                        )
                    try:
                        await asyncio.wait_for(
                            stop.wait(),
                            timeout=max(interval, min(60, 2 ** min(failures + 1, 6))),
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            await recovery.stop()
            notify_systemd("STOPPING=1\nSTATUS=Lifecycle recovery dispatcher stopped")
