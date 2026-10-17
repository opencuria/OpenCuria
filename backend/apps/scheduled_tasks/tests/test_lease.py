import asyncio

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.scheduled_tasks.lease import LEASE_KEY, LEASE_ROW_KEY, ScheduledTaskLease


@pytest.mark.django_db(transaction=True)
def test_sqlite_lease_is_exclusive_and_released():
    """Local SQLite keeps the previous process-exclusive singleton behavior."""
    from django.db import connections

    if connections["default"].vendor != "sqlite":
        pytest.skip("SQLite-specific compatibility coverage")

    connection = connections["default"]
    table = connection.ops.quote_name("scheduled_tasks_scheduler_lease")
    # Test database fixtures intentionally don't run this newly authored
    # migration in every checkout's prebuilt SQLite DB. This test constrains
    # the lease layer separately from the migration contract.
    with connection.cursor() as cursor:
        try:
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN deployment_version "
                "varchar(255) NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        try:
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN owner_id "
                "varchar(128) NOT NULL DEFAULT 'local'"
            )
        except Exception:
            pass
        try:
            cursor.execute(
                f"INSERT OR IGNORE INTO {table} "
                "(key, owner_token, expires_at, deployment_version, owner_id) "
                "VALUES (%s, NULL, %s, '', 'local')",
                [LEASE_ROW_KEY, timezone.now()],
            )
        except Exception:
            cursor.execute(
                f"INSERT OR IGNORE INTO {table} (key, owner_token, expires_at) "
                "VALUES (%s, NULL, %s)",
                [LEASE_ROW_KEY, timezone.now()],
            )

    async def scenario():
        from django.db import close_old_connections

        close_old_connections()
        first = ScheduledTaskLease()
        second = ScheduledTaskLease()
        try:
            assert await first.acquire()
            assert not await second.acquire()
            assert await first.renew()
        finally:
            await first.release()
            await second.release()
        third = ScheduledTaskLease()
        try:
            assert await third.acquire()
        finally:
            await third.release()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_postgres_renew_checks_advisory_lock_and_generation_row():
    lease = ScheduledTaskLease(deployment_version="release-a", owner_id="scheduler-a")
    lease._held = True
    lease._backend = "postgresql"
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchone(self):
            if "pg_locks" in calls[-1][0]:
                return (True,)
            return (1,)

        @property
        def rowcount(self):
            return 1

    class FakeConnection:
        vendor = "postgresql"
        alias = "default"

        class ops:
            @staticmethod
            def quote_name(value):
                return f'"{value}"'

        def cursor(self):
            return FakeCursor()

    connection = FakeConnection()
    lease._connection = connection
    lease._connect = lambda: connection
    lease._table = lambda: '"scheduled_tasks_scheduler_lease"'
    try:
        assert await lease.renew()
        assert "pg_locks" in calls[0][0]
        assert calls[0][1] == [(LEASE_KEY >> 32) & 0xFFFFFFFF, LEASE_KEY & 0xFFFFFFFF]
        assert "deployment_version" in calls[1][0]
        assert calls[1][1][-2:] == ["release-a", "scheduler-a"]
    finally:
        lease._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_scheduler_lock_row_tracks_generation_and_unclean_previous_owner(
    monkeypatch,
):
    lease = ScheduledTaskLease(deployment_version="release-b", owner_id="scheduler-a")
    lease._held = True
    lease._backend = "postgresql"
    connection = _FakePostgresConnection()
    calls = connection.calls
    lease._connection = connection
    lease._connect = lambda: connection
    lease._table = lambda: '"scheduled_tasks_scheduler_lease"'

    from django.db import transaction

    class Atomic:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(transaction, "atomic", lambda using=None: Atomic())
    try:
        assert lease._claim_postgres_row()
        assert lease.recovered_previous_owner is True
        assert lease.previous_owner_version == "release-old"
        assert lease.previous_owner_id == "scheduler-old"
        update = next(call for call in calls if call[0].startswith('UPDATE "scheduled_tasks'))
        assert update[1][-2:] == ["scheduler-a", LEASE_ROW_KEY]
        assert "deployment_version" in update[0]
    finally:
        lease._executor.shutdown(wait=True, cancel_futures=True)


class _FakePostgresConnection:
    """Minimal fake response to the scheduler singleton row query."""

    vendor = "postgresql"
    alias = "default"
    calls: list[tuple[str, list]] = []

    class ops:
        @staticmethod
        def quote_name(value):
            return f'"{value}"'

    class _Cursor:
        rowcount = 1

        def __init__(self, calls):
            self.calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params=None):
            self.calls.append((sql, params or []))

        def fetchone(self):
            return (
                "old-token",
                timezone.now() - timedelta(seconds=100),
                "release-old",
                "scheduler-old",
            )

    def cursor(self):
        return self._Cursor(self.calls)
