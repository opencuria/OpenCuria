"""Real PostgreSQL migration lock and candidate-schema refusal tests."""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.deployments.management.commands.upgrade_database import LOCK_KEY
from apps.deployments.models import DatabaseSchemaState
from apps.deployments.services import register_release
from common.deployment_compatibility import ReleaseManifest


@pytest.mark.django_db(transaction=True)
def test_migration_advisory_lock_is_exclusive():
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL advisory lock coverage")
    from apps.deployments.management.commands.upgrade_database import Command

    other = connection.copy()
    other.ensure_connection()
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
        with pytest.raises(CommandError, match="Timed out"):
            Command()._acquire_lock(0)
    finally:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
        other.close()
    command = Command()
    command._acquire_lock(0)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
        assert cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
def test_future_schema_is_checked_before_migration(settings, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Production-only online migration command")
    old = ReleaseManifest("old-live", 1, 1, 1, 1, 1)
    new = ReleaseManifest("candidate", 1, 1, 2, 1, 2)
    register_release(old)
    DatabaseSchemaState.objects.create(id=1, schema_version=1)
    manifest = tmp_path / "release.json"
    mapping = dict(vars(new))
    manifest.write_text(json.dumps(mapping))
    settings.DEPLOYMENT_RELEASE = mapping
    with pytest.raises(CommandError, match="unsupported by retained"):
        call_command("upgrade_database", manifest=str(manifest), plan_only=True)
    assert DatabaseSchemaState.objects.get(id=1).schema_version == 1


@pytest.mark.django_db(transaction=True)
def test_bootstrap_flag_cannot_bypass_online_policy_on_existing_db(settings, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Production-only online migration command")
    mapping = dict(vars(ReleaseManifest("candidate", 1, 1, 1, 1, 1)))
    settings.DEPLOYMENT_RELEASE = mapping
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps(mapping))
    with pytest.raises(CommandError, match="only allowed on an empty"):
        call_command(
            "upgrade_database",
            manifest=str(manifest),
            allow_initial_maintenance_bootstrap=True,
        )
