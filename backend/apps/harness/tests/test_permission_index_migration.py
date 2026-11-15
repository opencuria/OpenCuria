"""Exercise the permission lookup index on a populated pre-index database."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_permission_index_migrates_without_changing_requests(tmp_path: Path) -> None:
    """Forward, rollback and reapply preserve every pending and settled column."""
    script = """
import uuid
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

before = [("harness", "0040_remove_codex_turn_diff_snapshots")]
after = [("harness", "0041_permission_request_lookup_index")]
executor = MigrationExecutor(connection)
executor.migrate(before)
model = executor.loader.project_state(before).apps.get_model(
    "harness", "PermissionRequest"
)
for status in ("pending", "approved", "rejected"):
    model.objects.create(
        organization_id=uuid.uuid4(), session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(), message_id=uuid.uuid4(),
        call_id="call", tool="bash", pattern="synthetic command", title="Review",
        status=status, remember="once",
    )
original = list(model.objects.order_by("id").values())
for targets, present in ((after, True), (before, False), (after, True)):
    MigrationExecutor(connection).migrate(targets)
    assert list(model.objects.order_by("id").values()) == original
    with connection.cursor() as cursor:
        indexes = connection.introspection.get_constraints(cursor, model._meta.db_table)
    index = indexes.get("harness_perm_session_state_idx")
    assert bool(index) == present
    if index:
        assert index["columns"] == ["session_id", "status", "created_at"]
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "SQLITE_PATH": str(tmp_path / "permission-index.sqlite3"),
        },
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
