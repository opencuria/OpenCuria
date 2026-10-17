"""Single-owner lifecycle recovery dispatcher lease tests."""

from __future__ import annotations

import pytest
from django.test import TestCase

from apps.runners.services import recovery
from apps.runners.services.recovery import RECOVERY_LEASE_KEY, RecoveryService


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_recovery_service_exclusively_holds_postgres_advisory_lease(monkeypatch):
    service = RecoveryService("owner-a")
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params):
            calls.append((sql, params))
            if "LifecycleRecoveryLease" in sql:
                calls.append(("LifecycleRecoveryLease", []))

        def fetchone(self):
            if "SELECT owner_id" in calls[-1][0]:
                return None
            return (True,)

    class Connection:
        alias = "default"
        close_at = None

        def inc_thread_sharing(self):
            pass

        def ensure_connection(self):
            calls.append(("ensure_connection", []))

        def copy(self):
            return self

        def cursor(self):
            return Cursor()

        def close(self):
            calls.append(("close", []))

    connection = Connection()

    def connect(**kwargs):
        calls.append(("connect", [kwargs]))
        return connection

    class FakePsycopg:
        @staticmethod
        def connect(**kwargs):
            return connect(**kwargs)

    from django.conf import settings
    from django.db import connection as django_connection

    monkeypatch.setattr(django_connection, "vendor", "postgresql")
    monkeypatch.setattr(django_connection, "copy", lambda: connection)
    monkeypatch.setattr(
        settings,
        "DATABASES",
        {"default": {"NAME": "db", "USER": "u", "PASSWORD": "p"}},
    )
    await service.start()
    lock_calls = [call for call in calls if "pg_try_advisory_lock" in call[0]]
    assert lock_calls[0][1] == [RECOVERY_LEASE_KEY]
    assert service._lease_connection is connection
    await service.stop()
    assert any("pg_advisory_unlock" in call[0] for call in calls)
    assert calls[-1][0] == "close"
