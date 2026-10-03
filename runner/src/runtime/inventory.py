"""Observed physical storage graph. Null sizes never mean zero."""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class StorageResource:
    resource_id: str
    kind: str
    managed: bool
    aliases: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    state: str = "unknown"
    allocated_bytes: int | None = None
    logical_bytes: int | None = None
    virtual_bytes: int | None = None
    shared_bytes: int | None = None
    reclaimable_bytes: int | None = None
    provenance: str = "inspection"
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimeInventory:
    runtime_type: str
    complete: bool = False
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resources: list[StorageResource] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    filesystems: list[dict] = field(default_factory=list)
    foreign_resource_count: int = 0
