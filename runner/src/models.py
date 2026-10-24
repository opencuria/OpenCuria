"""Lightweight dataclasses for in-memory workspace state.

The runner no longer uses a local database. All workspace state is
derived from libvirt and cached
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
    credentials_present: bool | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DesktopSession:
    """In-memory representation of an active KasmVNC desktop session.

    The Xvnc process is shared. Independent leases decide whether it
    stays up: viewer, computer-use, and MCP owners are projected from the
    durable lease store. The process stops only when no holders remain.

    ``generation`` identifies the Xvnc process incarnation: it is 0 for
    unknown/recovered sessions and increments on every fresh Xvnc
    start. Callers compare it by identity to detect stale restarts
    (recovery/cold start) versus idempotent reuse of the same process.
    """

    workspace_id: uuid.UUID
    instance_id: str
    display: str = ":1"
    port: int = 6901
    viewer_held: bool = False
    computeruse_run_ids: set[str] = field(default_factory=set)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    generation: int = 0
    mcp_active: bool = False
    holder_count: int = 0
