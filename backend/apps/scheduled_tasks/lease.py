"""Database-backed ownership lock for the single scheduled-task worker."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from django.db import connections
from django.utils import timezone

LEASE_KEY = 7_396_102_481
LEASE_ROW_KEY = 1
LEASE_SECONDS = 45


class ScheduledTaskLease:
    """Hold a PostgreSQL advisory lock or a best-effort SQLite lease.

    All connection operations run on one dedicated thread so the database
    connection holding the advisory lock remains open and thread-affine for its
    entire lifetime. SQLite lacks advisory locks, so it uses a short TTL row
    renewed by the scheduler heartbeat.
    """

    def __init__(self) -> None:
        self.owner_token = uuid.uuid4()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scheduled-task-lease"
        )
        self._connection: Any | None = None
        self._backend = ""
        self._held = False

    async def acquire(self) -> bool:
        """Claim exclusive ownership before any scheduler recovery work."""
        try:
            return await self._submit(self._acquire)
        except BaseException:
            try:
                await self._submit(self._close)
            finally:
                self._executor.shutdown(wait=True, cancel_futures=True)
            raise

    async def renew(self) -> bool:
        """Renew SQLite ownership or confirm the advisory lock remains held."""
        return await self._submit(self._renew)

    async def release(self) -> None:
        """Release and close the dedicated database connection."""
        try:
            await self._submit(self._release)
        finally:
            if self._connection is not None:
                await self._submit(self._close)
            self._executor.shutdown(wait=True, cancel_futures=True)

    async def _submit(self, function):
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, function)

    def _connect(self):
        if self._connection is None:
            self._connection = connections["default"].copy()
            self._connection.close_at = None
            self._connection.ensure_connection()
            self._backend = self._connection.vendor
        return self._connection

    def _acquire(self) -> bool:
        connection = self._connect()
        if self._backend == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [LEASE_KEY])
                self._held = bool(cursor.fetchone()[0])
            if not self._held:
                self._close()
            return self._held
        if self._backend == "sqlite":
            now = timezone.now()
            expiry = now + timedelta(seconds=LEASE_SECONDS)
            table = connection.ops.quote_name("scheduled_tasks_scheduler_lease")
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT OR IGNORE INTO {table} (key, owner_token, expires_at) "
                    "VALUES (%s, %s, %s)",
                    [LEASE_ROW_KEY, str(self.owner_token), expiry],
                )
                cursor.execute(
                    f"UPDATE {table} SET owner_token = %s, expires_at = %s "
                    "WHERE key = %s AND (owner_token = %s OR expires_at <= %s)",
                    [
                        str(self.owner_token),
                        expiry,
                        LEASE_ROW_KEY,
                        str(self.owner_token),
                        now,
                    ],
                )
                self._held = cursor.rowcount == 1
            if not self._held:
                self._close()
            return self._held
        self._close()
        raise RuntimeError(
            f"Scheduled-task lease does not support database backend {self._backend!r}"
        )

    def _renew(self) -> bool:
        if not self._held:
            return False
        if self._backend == "postgresql":
            connection = self._connect()
            key_high = (LEASE_KEY >> 32) & 0xFFFFFFFF
            key_low = LEASE_KEY & 0xFFFFFFFF
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted "
                    "AND pid = pg_backend_pid() AND classid = %s::oid "
                    "AND objid = %s::oid AND objsubid = 1"
                    ")",
                    [key_high, key_low],
                )
                self._held = bool(cursor.fetchone()[0])
            return self._held
        connection = self._connect()
        table = connection.ops.quote_name("scheduled_tasks_scheduler_lease")
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET expires_at = %s WHERE key = %s "
                "AND owner_token = %s",
                [
                    now + timedelta(seconds=LEASE_SECONDS),
                    LEASE_ROW_KEY,
                    str(self.owner_token),
                ],
            )
            self._held = cursor.rowcount == 1
        return self._held

    def _release(self) -> None:
        if not self._held:
            self._close()
            return
        if self._backend == "postgresql":
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LEASE_KEY])
        elif self._backend == "sqlite":
            table = self._connection.ops.quote_name("scheduled_tasks_scheduler_lease")
            with self._connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET owner_token = NULL, expires_at = %s "
                    "WHERE key = %s AND owner_token = %s",
                    [timezone.now(), LEASE_ROW_KEY, str(self.owner_token)],
                )
        self._held = False
        self._close()

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
