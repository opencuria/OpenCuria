import asyncio

import pytest

from apps.scheduled_tasks.lease import LEASE_KEY, ScheduledTaskLease


@pytest.mark.django_db(transaction=True)
def test_sqlite_lease_is_exclusive_and_released():
    async def scenario():
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
async def test_postgres_renew_checks_database_lock_and_server_connection():
    lease = ScheduledTaskLease()
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
            return (True,)

    class FakeConnection:
        vendor = "postgresql"

        def cursor(self):
            return FakeCursor()

    connection = FakeConnection()
    lease._connection = connection
    lease._connect = lambda: connection
    try:
        assert await lease.renew()
        assert calls
        sql, params = calls[0]
        assert "pg_locks" in sql and "pg_backend_pid()" in sql
        assert params == [(LEASE_KEY >> 32) & 0xFFFFFFFF, LEASE_KEY & 0xFFFFFFFF]

        class LostCursor(FakeCursor):
            def fetchone(self):
                return (False,)

        connection.cursor = LostCursor
        assert not await lease.renew()
        assert lease._held is False
    finally:
        lease._executor.shutdown(wait=True, cancel_futures=True)
