"""Lightweight dataclasses for in-memory workspace state.

The runner no longer uses a local database. All workspace state is
derived from the runtime (Docker daemon, libvirt, …) and cached
in memory via these dataclasses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class WorkspaceInfo:
    """In-memory representation of a workspace managed by this runner.

    Populated from runtime metadata on startup and updated
    as workspace lifecycle events occur.
    """

    workspace_id: uuid.UUID
    instance_id: str
    status: str  # "running", "exited", "creating", etc.
    runtime_type: str = "docker"  # "docker" or "qemu"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
