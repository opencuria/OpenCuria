"""Native restart repair requires process exclusion beyond an expiring DB lease."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from common.process_ownership import BackendProcessGuard, backend_process_lock_path


def test_live_process_keeps_lock_until_descriptor_closes(tmp_path):
    path = tmp_path / "backend.asgi.lock"
    owner = BackendProcessGuard(path)
    contender = BackendProcessGuard(path)
    owner.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another live backend process"):
            contender.acquire()
        with pytest.raises(RuntimeError, match="already started"):
            owner.acquire()
    finally:
        owner.release_failed_start()
    contender.acquire()
    contender.release_failed_start()
    assert path.exists()  # Reusing the same inode prevents split ownership.


def test_lock_identity_follows_sqlite_data_and_supports_shared_override(
    tmp_path, settings, monkeypatch
):
    monkeypatch.delenv("BACKEND_PROCESS_LOCK_PATH", raising=False)
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": tmp_path / "db"}
    }
    assert backend_process_lock_path() == tmp_path / "db.asgi.lock"
    settings.DATABASES["default"]["NAME"] = tmp_path / "other"
    assert backend_process_lock_path() == tmp_path / "other.asgi.lock"
    monkeypatch.setenv("BACKEND_PROCESS_LOCK_PATH", str(tmp_path / "shared-lock"))
    assert backend_process_lock_path() == tmp_path / "shared-lock"


@pytest.mark.asyncio
async def test_expired_database_lease_cannot_start_repair_while_owner_lives(
    tmp_path, monkeypatch
):
    from config import asgi

    owner = BackendProcessGuard(tmp_path / "backend.lock")
    contender = BackendProcessGuard(owner.path)
    owner.acquire()

    class FakeScheduler:
        # A database lease could already be expired or its connection lost.
        start = AsyncMock()
        stop = AsyncMock()

    class FakeRecovery:
        start = AsyncMock()
        stop = AsyncMock()

    monkeypatch.setattr(
        "common.process_ownership.acquire_backend_process_guard", contender.acquire
    )
    monkeypatch.setattr(
        "apps.scheduled_tasks.services.ScheduledTaskScheduler", FakeScheduler
    )
    monkeypatch.setattr(
        "apps.harness.recovery_worker.HarnessRecoveryWorker", FakeRecovery
    )
    incoming = asyncio.Queue()
    await incoming.put({"type": "lifespan.startup"})
    outgoing = []

    async def send(message):
        outgoing.append(message)

    try:
        await asgi.application({"type": "lifespan"}, incoming.get, send)
        FakeScheduler.start.assert_not_awaited()
        FakeRecovery.start.assert_not_awaited()
        assert outgoing[0]["type"] == "lifespan.startup.failed"
        assert "Another live backend process" in outgoing[0]["message"]
    finally:
        owner.release_failed_start()
