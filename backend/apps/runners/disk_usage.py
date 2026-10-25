"""Workspace filesystem usage reported with runner VM metrics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import Workspace


@dataclass(frozen=True)
class WorkspaceDiskUsage:
    """Filesystem usage observed inside a workspace VM."""

    used_bytes: int
    free_bytes: int
    total_bytes: int
    observed_at: datetime

    @classmethod
    def from_workspace(cls, workspace: Workspace) -> WorkspaceDiskUsage | None:
        """Return the last persisted observation, or None if never measured."""
        observed_at = getattr(workspace, "disk_usage_observed_at", None)
        if observed_at is None:
            return None
        return cls(
            used_bytes=workspace.disk_used_bytes or 0,
            free_bytes=workspace.disk_free_bytes or 0,
            total_bytes=workspace.disk_total_bytes or 0,
            observed_at=observed_at,
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialize for frontend realtime events (matches the REST schema)."""
        return {
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "observed_at": self.observed_at.isoformat(),
        }


def parse_vm_disk_usage(
    vm_metrics: dict[str, Any] | None, *, observed_at: datetime
) -> dict[uuid.UUID, WorkspaceDiskUsage]:
    """Extract guest filesystem usage per workspace from a VM metrics payload.

    The runner only reports ``guest_disk_*`` counters while a guest answers;
    entries without complete, plausible counters are skipped.
    """
    usages: dict[uuid.UUID, WorkspaceDiskUsage] = {}
    for workspace_id, metric in (vm_metrics or {}).items():
        if not isinstance(metric, dict):
            continue
        try:
            key = uuid.UUID(str(workspace_id))
            usage = WorkspaceDiskUsage(
                used_bytes=int(metric["guest_disk_used_bytes"]),
                free_bytes=int(metric["guest_disk_free_bytes"]),
                total_bytes=int(metric["guest_disk_total_bytes"]),
                observed_at=observed_at,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if usage.total_bytes <= 0 or min(usage.used_bytes, usage.free_bytes) < 0:
            continue
        usages[key] = usage
    return usages
