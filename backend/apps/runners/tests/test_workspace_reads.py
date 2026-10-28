"""Workspace read composition consumes persistence through its injected ports."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.runners.services.workspace_reads import WorkspaceReadService
from apps.runners.workspace_read_repository import WorkspaceReadRelations


@pytest.mark.parametrize(
    ("annotated", "status", "expected"),
    [(True, "offline", True), (False, "online", False), (None, "online", True)],
)
def test_workspace_reads_use_loaded_relations_and_preserve_annotations(
    annotated: bool | None, status: str, expected: bool
) -> None:
    """Compose without database access or workspace foreign-key traversal."""
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        runner_id=uuid.uuid4(),
        created_by_id=1,
        name="Read state",
        status="running",
        active_operation=None,
        intervention_required=False,
        lifecycle_diagnostic="",
        qemu_vcpus=None,
        qemu_memory_mb=None,
        qemu_disk_size_gb=None,
        desktop_width=1280,
        desktop_height=720,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
        has_active_harness_session=True,
    )
    base = SimpleNamespace(id=uuid.uuid4())
    pending = SimpleNamespace(id=uuid.uuid4())
    repository = Mock()
    related = WorkspaceReadRelations(
        annotated_runner_online=annotated,
        runner_status=status,
        auto_stop_timeout_minutes=None,
        base_image=base,
        pending_base_image=pending,
        base_image_definition_name="Recipe",
    )
    repository.load_relations_many.return_value = {workspace.id: related}
    credentials, plugins, versions = Mock(), Mock(), Mock()
    credential_ids, plugin_ids = [uuid.uuid4()], [uuid.uuid4()]
    credentials.workspace_credential_ids_many.return_value = {
        workspace.id: credential_ids
    }
    plugins.list_workspace_plugin_ids_many.return_value = {workspace.id: plugin_ids}
    versions.version_refs.return_value = {
        base.id: {"id": "base"},
        pending.id: {"id": "pending"},
    }
    service = WorkspaceReadService(
        repository=repository,
        credentials=credentials,
        plugins=plugins,
        versions=versions,
    )

    data = service.describe(workspace)

    repository.load_relations_many.assert_called_once_with([workspace])
    credentials.workspace_credential_ids_many.assert_called_once_with([workspace.id])
    plugins.list_workspace_plugin_ids_many.assert_called_once_with([workspace.id])
    versions.version_refs.assert_called_once_with([base, pending])
    assert data["runner_online"] is expected
    assert data["has_active_session"] is True
    assert data["base_image"] == {"id": "base"}
    assert data["pending_base_image"] == {"id": "pending"}
    assert data["base_image_name"] == "Recipe"
    assert data["credential_ids"] == credential_ids
    assert data["plugin_ids"] == plugin_ids
    assert data["auto_stop_at"] is data["disk_usage"] is None
