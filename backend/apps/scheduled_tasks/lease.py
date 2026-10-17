"""Database-backed ownership lock for the single scheduled-task worker."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from django.db import close_old_connections, connections, transaction
from django.utils import timezone

LEASE_KEY = 7_396_102_481
LEASE_ROW_KEY = 1
LEASE_SECONDS = 45


class ScheduledTaskLease:
    """Hold a PostgreSQL advisory lock and generation row, or SQLite TTL lease."""

    def __init__(self, *, deployment_version: str = "", owner_id: str = "local") -> None:
        self.owner_token = uuid.uuid4()
        self.deployment_version = deployment_version
        self.owner_id = owner_id
        self.recovered_previous_owner = False
        self.previous_owner_version = ""
        self.previous_owner_id = ""
        self._clean_handoff = False
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scheduled-task-lease"
        )
        self._connection: Any | None = None
        self._backend = ""
        self._failure = ""
        self._held = False
        self._closed = False

    async def acquire(self) -> bool:
        """Claim exclusive ownership before scheduler recovery work."""
        try:
            acquired = await self._submit(self._acquire)
        except BaseException:
            await self._shutdown_executor()
            raise
        if not acquired:
            await self._shutdown_executor()
        else:
            await self._submit(close_old_connections)
        return acquired

    async def _shutdown_executor(self) -> None:
        """Close a pinned connection before stopping its dedicated worker thread."""
        if self._connection is not None:
            await self._submit(self._close)
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._closed = True

    async def renew(self) -> bool:
        """Renew the owner row and confirm the PostgreSQL advisory lock."""
        return bool(await self._submit(self._renew))

    async def release(self) -> None:
        """Publish a clean handoff before releasing exclusive ownership."""
        if self._closed:
            return
        try:
            if self._held:
                await self._submit(self._release)
        finally:
            if self._connection is not None:
                await self._submit(self._close)
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._closed = True

    async def _submit(self, function):
        import asyncio

        if self._closed:
            return False
        return await asyncio.get_running_loop().run_in_executor(self._executor, function)

    def _connect(self):
        if self._connection is None:
            self._connection = connections["default"].copy()
            self._connection.close_at = None
            self._connection.ensure_connection()
            self._backend = self._connection.vendor
        return self._connection

    def _table(self) -> str:
        return self._connection.ops.quote_name("scheduled_tasks_scheduler_lease")

    def _acquire(self) -> bool:
        connection = self._connect()
        if self._backend == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [LEASE_KEY])
                if not cursor.fetchone()[0]:
                    self._close()
                    return False
            self._held = True
            if not self._claim_postgres_row():
                self._unlock_postgres()
                self._held = False
                self._close()
                return False
            return True
        if self._backend == "sqlite":
            return self._claim_sqlite_row()
        self._close()
        raise RuntimeError(
            f"Scheduled-task lease does not support database backend {self._backend!r}"
        )

    def _claim_postgres_row(self) -> bool:
        """Associate a release/process owner or wait out an unclean prior owner."""
        table = self._table()
        now = timezone.now()
        expiry = now + timedelta(seconds=LEASE_SECONDS)
        with transaction.atomic(using=self._connection.alias):
            with self._connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT owner_token, expires_at, deployment_version, owner_id "
                    f"FROM {table} WHERE key = %s FOR UPDATE",
                    [LEASE_ROW_KEY],
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"INSERT INTO {table} "
                        "(key, owner_token, expires_at, deployment_version, owner_id) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [
                            LEASE_ROW_KEY,
                            str(self.owner_token),
                            expiry,
                            self.deployment_version,
                            self.owner_id,
                        ],
                    )
                    self._clean_handoff = True
                    return True
                previous_token, previous_expiry, previous_version, previous_owner_id = row
                if previous_token is not None and previous_expiry > now:
                    return False
                self.recovered_previous_owner = previous_token is not None
                self._clean_handoff = previous_token is None
                self.previous_owner_version = previous_version or ""
                self.previous_owner_id = previous_owner_id or ""
                cursor.execute(
                    f"UPDATE {table} SET owner_token=%s, expires_at=%s, "
                    "deployment_version=%s, owner_id=%s WHERE key=%s",
                    [
                        str(self.owner_token),
                        expiry,
                        self.deployment_version,
                        self.owner_id,
                        LEASE_ROW_KEY,
                    ],
                )
                return cursor.rowcount == 1

    def _claim_sqlite_row(self) -> bool:
        """Preserve the original local SQLite scheduler lease behavior."""
        connection = self._connection
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
                f"UPDATE {table} SET owner_token=%s, expires_at=%s "
                "WHERE key=%s AND (owner_token=%s OR expires_at<=%s)",
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
            self._failure = "SQLite lease update did not claim the singleton row"
            self._close()
        return self._held

    def _renew(self) -> bool:
        if not self._held:
            return False
        connection = self._connect()
        table = self._table()
        with connection.cursor() as cursor:
            if self._backend == "postgresql":
                high, low = (LEASE_KEY >> 32) & 0xFFFFFFFF, LEASE_KEY & 0xFFFFFFFF
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND granted AND pid=pg_backend_pid() AND classid=%s::oid "
                    "AND objid=%s::oid AND objsubid=1)",
                    [high, low],
                )
                if not cursor.fetchone()[0]:
                    self._held = False
                    return False
                cursor.execute(
                    f"UPDATE {table} SET expires_at=%s WHERE key=%s "
                    "AND owner_token=%s AND deployment_version=%s AND owner_id=%s",
                    [
                        timezone.now() + timedelta(seconds=LEASE_SECONDS),
                        LEASE_ROW_KEY,
                        str(self.owner_token),
                        self.deployment_version,
                        self.owner_id,
                    ],
                )
            else:
                cursor.execute(
                    f"UPDATE {table} SET expires_at=%s WHERE key=%s AND owner_token=%s",
                    [
                        timezone.now() + timedelta(seconds=LEASE_SECONDS),
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
        table = self._table()
        with self._connection.cursor() as cursor:
            if self._backend == "postgresql":
                cursor.execute(
                    f"UPDATE {table} SET owner_token=NULL, expires_at=%s, "
                    "deployment_version='', owner_id='' "
                    "WHERE key=%s AND owner_token=%s AND owner_id=%s",
                    [timezone.now(), LEASE_ROW_KEY, str(self.owner_token), self.owner_id],
                )
            else:
                cursor.execute(
                    f"UPDATE {table} SET owner_token=NULL, expires_at=%s "
                    "WHERE key=%s AND owner_token=%s",
                    [timezone.now(), LEASE_ROW_KEY, str(self.owner_token)],
                )
        if self._backend == "postgresql":
            self._unlock_postgres()
        self._held = False
        self._close()

    def _unlock_postgres(self) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LEASE_KEY])

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
