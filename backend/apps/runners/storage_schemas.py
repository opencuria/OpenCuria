"""Sanitized runner-storage presentation contract; no raw manifests or recipes."""

from datetime import datetime
from typing import Any

from ninja import Schema


class StorageWorkspaceOut(Schema):
    id: str
    name: str
    owner_id: str | None
    owner_label: str
    status: str
    last_activity_at: datetime | None
    observed_state: str | None = None
    base_image_instance_id: str | None = None
    pending_base_image_instance_id: str | None = None
    allocated_bytes: int | None = None


class StorageResourceOut(Schema):
    physical_id: str
    kind: str
    managed: bool
    state: str
    allocated_bytes: int | None
    logical_bytes: int | None
    virtual_bytes: int | None
    shared_bytes: int | None
    reclaimable_bytes: int | None
    aliases: list[str]
    dependencies: list[str]
    image_id: str | None
    workspace: StorageWorkspaceOut | None
    provenance: str
    filesystem_id: str | None = None
    file_identity: str | None = None


class StorageRuntimeOut(Schema):
    runtime_type: str
    fresh: bool
    snapshot_id: int | None
    epoch: str | None
    sequence: int | None
    collected_at: datetime | None
    received_at: datetime | None
    filesystems: list[dict[str, Any]]
    resources: list[StorageResourceOut]
    diagnostics: dict[str, Any] | None


class StorageGenerationOut(Schema):
    id: str
    name: str
    owner_id: str | None
    owner_label: str
    runtime_type: str
    build_job_id: str | None
    definition_id: str | None
    definition_name: str | None
    generation: int | None
    captured_image_id: str | None = None
    line_name: str | None = None
    message: str = ""
    is_latest: bool = False
    retention: str | None = None
    status: str
    runner_ref: str
    size_bytes: int | None
    origin_type: str
    revision_id: str | None
    is_legacy: bool
    build_job__current_generation_id: str | None
    is_current: bool
    is_pending: bool
    assignment_status: str | None
    observed_state: str
    size_source: str
    dependencies: list[StorageWorkspaceOut]


class RunnerStorageOut(Schema):
    runner_id: str
    runner_online: bool
    latest_snapshot_id: int | None
    latest_complete: bool
    runtimes: list[StorageRuntimeOut]
    generations: list[StorageGenerationOut]
    capture_requests: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    pin_diagnostics: list[dict[str, Any]]
