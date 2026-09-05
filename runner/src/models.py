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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DesktopSession:
    """In-memory representation of an active KasmVNC desktop session.

    The Xvnc process is shared. Independent leases decide whether it
    stays up: a viewer hold (manual VNC) and zero or more computer-use
    ``run_id`` holds. The process is stopped only when both are empty.
    """

    workspace_id: uuid.UUID
    instance_id: str
    display: str = ":1"
    port: int = 6901
    viewer_held: bool = False
    computeruse_run_ids: set[str] = field(default_factory=set)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DesktopReleaseResult:
    """Outcome of releasing a desktop lease or force-stopping the process."""

    stopped: bool
    process_alive: bool
    viewer_held: bool
    computer_use_active: bool
