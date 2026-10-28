"""Tests for the canonical DesktopManager.desktop_action."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.service import WorkspaceService
from src.services.sessions.desktop import DesktopManager


def test_desktop_input_validation_and_geometry_bounds() -> None:
    assert DesktopManager._sanitize_run_id("run-1") == "run-1"
    for run_id in ("", "bad id!", "../escape"):
        with pytest.raises(ValueError, match="Invalid run_id"):
            DesktopManager._sanitize_run_id(run_id)
    assert DesktopManager._resolve_desktop_geometry() == (1920, 1080)
    assert DesktopManager._resolve_desktop_geometry(1281, 721) == (1280, 720)
    assert DesktopManager._resolve_desktop_geometry(1, 99999) == (800, 2160)


class DummyRuntime:
    """Minimal runtime stub for desktop action tests."""

    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock()


def _workspace_id(service: WorkspaceService) -> uuid.UUID:
    return next(iter(service.registry._cache))


def _runtime(service: WorkspaceService) -> DummyRuntime:
    return service.registry.get_runtime(_workspace_id(service))  # type: ignore[return-value]


@pytest.fixture
def service(tmp_path) -> WorkspaceService:
    runtime = DummyRuntime()
    svc = WorkspaceService(
        runtime=runtime, settings=RunnerSettings(state_dir=str(tmp_path))
    )
    svc.configure_desktop_leases(str(tmp_path), "desktop-test-epoch")
    workspace_id = uuid.uuid4()
    svc.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
    )
    return svc


@pytest.mark.parametrize("action", ["hold", "release"])
async def test_ownership_actions_require_lease_identity(service, action) -> None:
    with pytest.raises(ValueError, match="lease_id is required"):
        await service.desktop.desktop_action(_workspace_id(service), action)
    _runtime(service).exec_command_wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_start_rejects_path_outside_workspace_for_durable_owner(
    service: WorkspaceService,
) -> None:
    workspace_id = _workspace_id(service)
    lease_id = "computeruse-lease"
    await service.desktop.lease_store.reserve(
        str(workspace_id),
        "instance-1",
        lease_id,
        "computeruse",
        "run-1",
        service.desktop.epoch,
        1,
        180,
    )
    _runtime(service).exec_command_wait.return_value = (0, "alive")

    with pytest.raises(ValueError, match="/workspace"):
        await service.desktop.desktop_action(
            workspace_id,
            "record_start",
            {"lease_id": lease_id, "run_id": "run-1", "path": "/etc/passwd"},
        )

    assert await service.desktop.lease_store.recordings(lease_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["bogus", "screenshot", "display_info", "execute"])
async def test_unknown_action_raises(service: WorkspaceService, action: str) -> None:
    _runtime(service).exec_command_wait.return_value = (0, "alive")

    with pytest.raises(ValueError, match=f"Unknown desktop action: {action}"):
        await service.desktop.desktop_action(_workspace_id(service), action)
    _runtime(service).exec_command_wait.assert_not_awaited()


async def test_desktop_env_pins_xauthority(
    service: WorkspaceService,
) -> None:
    """Every X11 client resolves auth via the runner-owned file."""
    assert service.desktop._desktop_env() == {
        "HOME": "/root",
        "DISPLAY": ":1",
        "XAUTHORITY": "/root/.Xauthority",
    }
