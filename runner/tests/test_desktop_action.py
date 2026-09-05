"""Tests for WorkspaceService.desktop_action."""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.models import DesktopSession, WorkspaceInfo
from src.service import WorkspaceService


class DummyRuntime:
    """Minimal runtime stub for desktop action tests."""

    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock()

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"

    def get_workspace_network_name(self, workspace_id: str) -> str:
        return f"opencuria-ws-{workspace_id}"


@pytest.fixture
def service() -> WorkspaceService:
    runtime = DummyRuntime()
    svc = WorkspaceService(runtimes={"docker": runtime}, settings=RunnerSettings())
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    svc._workspace_id = workspace_id
    svc._runtime = runtime
    return svc


@pytest.mark.asyncio
async def test_ensure_calls_start_desktop(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "started"),
    ]

    result = await service.desktop_action(service._workspace_id, "ensure")

    assert result["ok"] is True
    assert result["display"] == ":1"
    assert service._workspace_id in service._desktop_sessions
    assert service._desktop_sessions[service._workspace_id].viewer_held is False


@pytest.mark.asyncio
async def test_ensure_returns_existing_live_session(service: WorkspaceService) -> None:
    service._desktop_sessions[service._workspace_id] = DesktopSession(
        workspace_id=service._workspace_id,
        instance_id="instance-1",
    )
    service._runtime.exec_command_wait.return_value = (0, "alive")

    result = await service.desktop_action(service._workspace_id, "ensure")

    assert result["ok"] is True
    assert service._runtime.exec_command_wait.await_count == 1


@pytest.mark.asyncio
async def test_hold_and_release_computer_use_lease(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, ""),
    ]

    held = await service.desktop_action(
        service._workspace_id,
        "hold",
        {"kind": "computeruse", "run_id": "run-1"},
    )
    assert held["ok"] is True
    assert held["computer_use"] is True
    session = service._desktop_sessions[service._workspace_id]
    assert "run-1" in session.computeruse_run_ids

    released = await service.desktop_action(
        service._workspace_id,
        "release",
        {"kind": "computeruse", "run_id": "run-1"},
    )
    assert released["ok"] is True
    assert released["stopped"] is True
    assert service._workspace_id not in service._desktop_sessions


@pytest.mark.asyncio
async def test_screenshot_happy_path(service: WorkspaceService) -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(jpeg).decode()),
    ]

    result = await service.desktop_action(service._workspace_id, "screenshot")

    assert result["ok"] is True
    assert result["mime"] == "image/jpeg"
    assert result["image_b64"] == base64.b64encode(jpeg).decode()
    assert result["width"] == 1920
    assert result["height"] == 1080


@pytest.mark.asyncio
async def test_move_uses_xdotool_with_display(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, ""),
    ]

    result = await service.desktop_action(
        service._workspace_id, "move", {"x": 10, "y": 20}
    )

    assert result == {"ok": True}
    call = service._runtime.exec_command_wait.await_args_list[-1]
    assert call.args[1] == ["sh", "-lc", "xdotool mousemove --sync 10 20"]
    assert call.kwargs["env"] == {"HOME": "/root", "DISPLAY": ":1"}


@pytest.mark.asyncio
async def test_click_uses_xdotool(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, ""),
    ]

    result = await service.desktop_action(
        service._workspace_id,
        "click",
        {"button": "left", "x": 5, "y": 6},
    )

    assert result == {"ok": True}
    call = service._runtime.exec_command_wait.await_args_list[-1]
    assert "xdotool mousemove --sync 5 6" in call.args[1][2]
    assert "xdotool click 1" in call.args[1][2]


@pytest.mark.asyncio
async def test_record_start_and_stop_happy_path(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, "12345\n"),
        (0, "alive"),
        (0, ""),
    ]

    start = await service.desktop_action(
        service._workspace_id,
        "record_start",
        {"run_id": "run-1"},
    )
    stop = await service.desktop_action(
        service._workspace_id,
        "record_stop",
        {"run_id": "run-1"},
    )

    assert start == {
        "ok": True,
        "run_id": "run-1",
        "path": "/workspace/.opencuria/computeruse/run-1/session.mp4",
    }
    assert stop == {
        "ok": True,
        "path": "/workspace/.opencuria/computeruse/run-1/session.mp4",
    }
    assert (service._workspace_id, "run-1") not in service._desktop_recordings


@pytest.mark.asyncio
async def test_screenshot_fails_when_desktop_down(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.return_value = (1, "dead")

    with pytest.raises(RuntimeError, match="Desktop session is not active"):
        await service.desktop_action(service._workspace_id, "screenshot")


@pytest.mark.asyncio
async def test_record_start_rejects_path_outside_workspace(
    service: WorkspaceService,
) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
    ]

    with pytest.raises(ValueError, match="/workspace"):
        await service.desktop_action(
            service._workspace_id,
            "record_start",
            {"run_id": "run-1", "path": "/etc/passwd"},
        )


@pytest.mark.asyncio
async def test_open_url_rejects_non_http(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.return_value = (0, "alive")

    with pytest.raises(ValueError, match="http"):
        await service.desktop_action(
            service._workspace_id,
            "open_url",
            {"url": "file:///etc/passwd"},
        )

    with pytest.raises(ValueError, match="http"):
        await service.desktop_action(
            service._workspace_id,
            "open_url",
            {"url": "javascript:alert(1)"},
        )


@pytest.mark.asyncio
async def test_screenshot_crop_happy_path(service: WorkspaceService) -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(jpeg).decode()),
    ]

    result = await service.desktop_action(
        service._workspace_id,
        "screenshot",
        {"crop_x": 10, "crop_y": 20, "crop_w": 100, "crop_h": 50},
    )

    assert result["width"] == 100
    assert result["height"] == 50
    call = service._runtime.exec_command_wait.await_args_list[-1]
    assert "crop=100:50:10:20" in call.args[1][2]


@pytest.mark.asyncio
async def test_screenshot_crop_rejects_invalid_bounds(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
    ]

    with pytest.raises(ValueError, match="Invalid screenshot crop bounds"):
        await service.desktop_action(
            service._workspace_id,
            "screenshot",
            {"crop_x": 1900, "crop_y": 0, "crop_w": 100, "crop_h": 100},
        )


@pytest.mark.asyncio
async def test_unknown_action_raises(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.return_value = (0, "alive")

    with pytest.raises(ValueError, match="Unknown desktop action"):
        await service.desktop_action(service._workspace_id, "bogus")
