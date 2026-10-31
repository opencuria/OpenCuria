"""Independent recovery work progresses while failing stages remain unhealthy."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.runners.credential_sync_repository import LegacyCredentialSyncRepository
from apps.runners.deletion_repository import DeletionRepository
from apps.runners.inventory_repository import InventoryRepository
from apps.runners.operations import OperationRepository
from apps.runners.services import RunnerService
from apps.runners.services.capture import CaptureService
from apps.runners.services.image_retention import ImageRetentionService
from apps.runners.services.recovery import RecoveryService
from apps.runners.services.recreate import RecreateService


@pytest.fixture
def stages(monkeypatch):
    """Replace persistence boundaries; these tests never access a database."""
    boundaries = {
        "legacy_credentials": (LegacyCredentialSyncRepository, "fence"),
        "deletion": (DeletionRepository, "tick"),
        "capture": (CaptureService, "tick"),
        "recreate": (RecreateService, "tick"),
        "retention": (ImageRetentionService, "tick"),
        "inventory": (InventoryRepository, "refresh_candidates"),
        "inspection": (OperationRepository, "list_for_inspection"),
        "commands": (OperationRepository, "candidates"),
    }
    mocks = {}
    for name, (owner, method) in boundaries.items():
        mocks[name] = Mock(return_value=[])
        monkeypatch.setattr(owner, method, mocks[name])
    command = SimpleNamespace(
        task_id="task",
        event="task:stop_workspace",
        task=SimpleNamespace(workspace_id=None, runner=SimpleNamespace(sid="runner")),
    )
    mocks["commands"].return_value = [command]
    monkeypatch.setattr(DeletionRepository, "delivery_allowed", Mock(return_value=True))
    monkeypatch.setattr(OperationRepository, "delivery_payload", Mock(return_value={}))
    monkeypatch.setattr(
        OperationRepository, "envelope", Mock(return_value={"operation_id": "task"})
    )
    return mocks


@pytest.mark.parametrize(
    "failing",
    [
        "legacy_credentials",
        "deletion",
        "capture",
        "recreate",
        "retention",
        "inventory",
        "inspection",
        "commands",
    ],
)
async def test_failed_stage_does_not_starve_others_and_never_reports_healthy(
    stages, failing
):
    stages[failing].side_effect = RuntimeError("temporary stage failure")
    transport = AsyncMock()
    with pytest.raises(RuntimeError, match=f"stages failed: {failing}"):
        await RecoveryService().tick(transport)
    for boundary in stages.values():
        boundary.assert_called_once()
    if failing != "commands":
        transport.emit.assert_awaited_once_with(
            "task:stop_workspace", {"operation_id": "task"}, room="runner"
        )
    stages[failing].side_effect = None
    # The next healthy tick must recover without restarting the worker object.
    await RecoveryService().tick(transport)


async def test_notification_failure_does_not_block_other_results_or_delivery(
    stages, monkeypatch
):
    stages["capture"].return_value = [
        {"workspace_id": "first", "status": "running", "diagnostic": ""},
        {"workspace_id": "second", "status": "running", "diagnostic": ""},
    ]
    notification = Mock(side_effect=[ConnectionError("frontend unavailable"), None])
    monkeypatch.setattr(RunnerService, "_forward_to_frontend", notification)
    completed = Mock()
    monkeypatch.setattr(RunnerService, "_forward_workspace_operation", completed)
    transport = AsyncMock()
    with pytest.raises(RuntimeError, match="stages failed: notification"):
        await RecoveryService().tick(transport)
    assert notification.call_count == 2
    completed.assert_called_once_with("second", None)
    transport.emit.assert_awaited_once()
