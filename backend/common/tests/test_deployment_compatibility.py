"""Tests for conservative release and online migration safety rules."""

from __future__ import annotations

import json

import pytest
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

from apps.deployments.services import require_compatible_releases
from common.deployment_compatibility import (
    AUDITED_MIGRATIONS,
    DeploymentUpgradeError,
    ReleaseManifest,
    migration_safety_error,
)

MANIFEST = {
    "version": "v1.2.3",
    "protocol_min": 2,
    "protocol_max": 4,
    "database_schema": 7,
    "database_schema_min": 6,
    "database_schema_max": 8,
}


def test_manifest_validation_rejects_mutable_or_inconsistent_data(tmp_path) -> None:
    """Manifest data is strict and compatibility ranges are explicit."""
    path = tmp_path / "release.json"
    path.write_text(json.dumps(MANIFEST))
    assert ReleaseManifest.from_file(path).version == "v1.2.3"
    with pytest.raises(DeploymentUpgradeError, match="schema"):
        ReleaseManifest.from_mapping({**MANIFEST, "database_schema": 9})
    with pytest.raises(DeploymentUpgradeError, match="protocol_min"):
        ReleaseManifest.from_mapping({**MANIFEST, "protocol_min": 5})
    with pytest.raises(DeploymentUpgradeError, match="unknown fields"):
        ReleaseManifest.from_mapping({**MANIFEST, "rollback": True})


def test_all_retained_generations_must_intersect() -> None:
    """Compatibility is checked across the complete retained set, not N-1."""
    releases = [
        ReleaseManifest.from_mapping({**MANIFEST, "version": f"v{i}"}) for i in range(4)
    ]
    require_compatible_releases(releases)
    with pytest.raises(DeploymentUpgradeError, match="protocol"):
        require_compatible_releases(
            [
                *releases,
                ReleaseManifest.from_mapping(
                    {
                        **MANIFEST,
                        "version": "old",
                        "protocol_min": 9,
                        "protocol_max": 10,
                    }
                ),
            ]
        )


def migration(*operations: migrations.operations.base.Operation, atomic: bool = True):
    """Build a minimal synthetic migration for classifier tests."""
    return type(
        "TestMigration",
        (),
        {
            "app_label": "testapp",
            "name": "0001_test",
            "operations": operations,
            "atomic": atomic,
        },
    )()


def test_online_policy_accepts_safe_additive_operations() -> None:
    """Creating a fresh table or nullable additive field is safe online."""
    fresh = migrations.CreateModel(
        name="Fresh",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("name", models.CharField(max_length=20)),
        ],
    )
    nullable = migrations.AddField(
        model_name="existing",
        name="extra",
        field=models.CharField(max_length=20, null=True),
    )
    safe_new_table = migration(
        fresh,
        migrations.AddIndex(
            model_name="fresh",
            index=models.Index(fields=["name"], name="fresh_name_idx"),
        ),
        migrations.AddConstraint(
            model_name="fresh",
            constraint=models.UniqueConstraint(fields=["name"], name="fresh_name_uniq"),
        ),
    )
    assert migration_safety_error(safe_new_table) is None
    assert migration_safety_error(migration(nullable)) is None


def test_online_policy_refuses_destructive_and_opaque_operations() -> None:
    """Unknown/data-destructive changes fail closed pending explicit review."""
    for operation in (
        migrations.DeleteModel(name="Customer"),
        migrations.RemoveField(model_name="customer", name="email"),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(fields=["email"], name="customer_email_idx"),
        ),
        migrations.RunSQL("UPDATE customer SET email = ''"),
        migrations.RunPython(lambda apps, schema_editor: None),
        migrations.AddField(
            model_name="customer",
            name="required",
            field=models.CharField(max_length=20),
        ),
    ):
        assert migration_safety_error(migration(operation)) is not None


def test_audited_migration_requires_specific_nonempty_reason() -> None:
    """Audits are migration-scoped, never a broad operation class exemption."""
    target = migration(migrations.RunSQL("SELECT 1"))
    key = (target.app_label, target.name)
    AUDITED_MIGRATIONS[key] = "reviewed harmless read-only SQL"
    try:
        assert migration_safety_error(target) is None
    finally:
        del AUDITED_MIGRATIONS[key]


def test_concurrent_index_requires_non_atomic_migration() -> None:
    """Concurrent index operations cannot run inside a migration transaction."""
    operation = AddIndexConcurrently(
        "customer", models.Index(fields=["id"], name="customer_id_idx")
    )
    assert "atomic = False" in migration_safety_error(migration(operation))
    assert migration_safety_error(migration(operation, atomic=False)) is None


def test_python_default_does_not_make_mixed_version_inserts_safe():
    """Old Django code omits new NOT NULL fields; Python defaults aren't DB defaults."""
    operation = migrations.AddField(
        model_name="existing",
        name="required",
        field=models.CharField(max_length=20, default=""),
    )
    assert migration_safety_error(migration(operation)) is not None
