"""Native restart repair requires process exclusion beyond an expiring DB lease."""

from __future__ import annotations

import pytest

from common.process_ownership import ExecutionProcessGuard, execution_process_lock_path


def test_live_process_keeps_lock_until_descriptor_closes(tmp_path):
    path = tmp_path / "backend.execution.lock"
    owner = ExecutionProcessGuard(path)
    contender = ExecutionProcessGuard(path)
    owner.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another live execution process"):
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
    monkeypatch.delenv("EXECUTION_PROCESS_LOCK_PATH", raising=False)
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": tmp_path / "db"}
    }
    assert execution_process_lock_path() == tmp_path / "db.execution.lock"
    settings.DATABASES["default"]["NAME"] = tmp_path / "other"
    assert execution_process_lock_path() == tmp_path / "other.execution.lock"
    monkeypatch.setenv("EXECUTION_PROCESS_LOCK_PATH", str(tmp_path / "shared-lock"))
    assert execution_process_lock_path() == tmp_path / "shared-lock"
