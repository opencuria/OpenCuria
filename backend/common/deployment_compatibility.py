"""Release compatibility manifests and conservative Django migration policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.db.migrations.operations.base import Operation


class DeploymentUpgradeError(Exception):
    """Raised when an online deployment cannot be proven safe."""


@dataclass(frozen=True)
class ReleaseManifest:
    """Compatibility promises for one immutable OpenCuria release."""

    version: str
    protocol_min: int
    protocol_max: int
    database_schema: int
    database_schema_min: int
    database_schema_max: int

    @classmethod
    def from_file(cls, path: str | Path) -> ReleaseManifest:
        """Load and validate a JSON release manifest."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentUpgradeError(
                f"Cannot read release manifest: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise DeploymentUpgradeError("Release manifest must be a JSON object")
        required = {
            "version",
            "protocol_min",
            "protocol_max",
            "database_schema",
            "database_schema_min",
            "database_schema_max",
        }
        missing = sorted(required - data.keys())
        extra = sorted(data.keys() - required)
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if extra:
                details.append(f"unknown fields: {', '.join(extra)}")
            raise DeploymentUpgradeError(
                "Invalid release manifest (" + "; ".join(details) + ")"
            )
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> ReleaseManifest:
        """Validate and construct a manifest from decoded JSON data."""
        expected = {
            "version",
            "protocol_min",
            "protocol_max",
            "database_schema",
            "database_schema_min",
            "database_schema_max",
        }
        if set(data) != expected:
            raise DeploymentUpgradeError("Manifest contains missing or unknown fields")
        version = data.get("version")
        if not isinstance(version, str) or not version.strip():
            raise DeploymentUpgradeError(
                "Release version must be a non-empty immutable tag or digest"
            )
        values: dict[str, int] = {}
        for name in (
            "protocol_min",
            "protocol_max",
            "database_schema",
            "database_schema_min",
            "database_schema_max",
        ):
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DeploymentUpgradeError(f"{name} must be a non-negative integer")
            values[name] = value
        if values["protocol_min"] > values["protocol_max"]:
            raise DeploymentUpgradeError("protocol_min must not exceed protocol_max")
        if values["database_schema_min"] > values["database_schema_max"]:
            raise DeploymentUpgradeError(
                "database_schema_min must not exceed database_schema_max"
            )
        if (
            not values["database_schema_min"]
            <= values["database_schema"]
            <= values["database_schema_max"]
        ):
            raise DeploymentUpgradeError(
                "database_schema must be within the release's supported schema range"
            )
        return cls(version=version.strip(), **values)


# Opaque RunPython/RunSQL migrations require an explicit migration review and
# rationale here. No unattended online migration has been approved yet.
AUDITED_MIGRATIONS: dict[tuple[str, str], str] = {}


def migration_safety_error(migration: Any) -> str | None:
    """Return why a migration is unsafe online, or ``None`` when proven safe.

    Unknown operations fail closed. A reviewed migration can be exempted only
    by an explicit entry in ``AUDITED_MIGRATIONS`` with its rationale.
    """
    key = (migration.app_label, migration.name)
    created_models: set[str] = set()
    for operation in migration.operations:
        model_name = getattr(operation, "model_name", "")
        operation_name = type(operation).__name__
        if operation_name == "CreateModel":
            created_models.add(operation.name.lower())
            continue
        if (
            model_name
            and model_name.lower() in created_models
            and operation_name
            in {
                "AddIndex",
                "AddConstraint",
                "AlterField",
            }
        ):
            continue
        reason = _operation_safety_error(operation)
        if operation_name == "AddIndexConcurrently" and migration.atomic:
            reason = "AddIndexConcurrently requires Migration.atomic = False"
        if reason:
            rationale = AUDITED_MIGRATIONS.get(key, "").strip()
            if rationale and operation_name in {"RunPython", "RunSQL"}:
                continue
            return f"{migration.app_label}.{migration.name}: {reason}"
    return None


def _operation_safety_error(operation: Operation) -> str | None:
    """Apply a conservative online-safety allowlist to one migration op."""
    name = type(operation).__name__
    if name == "CreateModel":
        # A freshly created table has no pre-existing rows to scan/backfill.
        # Indexes/constraints over that new empty table are safe DDL operations.
        return None
    if name == "AddField":
        field = operation.field
        if getattr(field, "primary_key", False):
            return "AddField adds a primary key to an existing table"
        if getattr(field, "unique", False) or getattr(field, "db_index", False):
            return "AddField adds an index/uniqueness constraint to an existing table"
        if getattr(field, "remote_field", None) is not None:
            return "AddField adds a relation constraint to an existing table"
        if getattr(field, "null", False):
            return None
        # A Django Python default is not a persistent DB default. Old
        # binaries omit this column on INSERT, so allowing a new NOT NULL
        # field here breaks mixed-version writers even without a heap rewrite.
        return (
            f"AddField adds non-null field {field.name!r}; old writers may omit it. "
            "Use an expand/backfill/contract rollout"
        )
    if name == "AddIndexConcurrently":
        return None
    if name in {"AddIndex", "AddConstraint"}:
        return f"{name} changes constraints/indexes on an existing table"
    if name in {"AlterModelOptions", "AlterModelManagers"}:
        return None
    if name == "RunPython":
        return "RunPython needs an explicitly audited migration exemption"
    if name == "RunSQL":
        return "RunSQL needs an explicitly audited migration exemption"
    return f"unreviewed operation {name}; online safety is not established"
