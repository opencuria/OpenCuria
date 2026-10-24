"""Tests for the canonical DesktopManager.desktop_action."""

from __future__ import annotations

import base64
import uuid
from typing import Any
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

    def get_guest_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"



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
async def test_screenshot_happy_path(service: WorkspaceService) -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(jpeg).decode()),
    ]

    result = await service.desktop.desktop_action(_workspace_id(service), "screenshot")

    assert result["ok"] is True
    assert result["mime"] == "image/jpeg"
    assert result["image_b64"] == base64.b64encode(jpeg).decode()
    assert result["width"] == 1920
    assert result["height"] == 1080


@pytest.mark.asyncio
async def test_screenshot_fails_when_desktop_down(service: WorkspaceService) -> None:
    _runtime(service).exec_command_wait.return_value = (1, "dead")

    with pytest.raises(RuntimeError, match="Desktop session is not active"):
        await service.desktop.desktop_action(_workspace_id(service), "screenshot")


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
async def test_screenshot_crop_happy_path(service: WorkspaceService) -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(jpeg).decode()),
    ]

    result = await service.desktop.desktop_action(
        _workspace_id(service),
        "screenshot",
        {"crop_x": 10, "crop_y": 20, "crop_w": 100, "crop_h": 50},
    )

    assert result["width"] == 100
    assert result["height"] == 50
    call = _runtime(service).exec_command_wait.await_args_list[-1]
    assert "crop=100:50:10:20" in call.args[1][2]


@pytest.mark.asyncio
async def test_screenshot_crop_rejects_invalid_bounds(service: WorkspaceService) -> None:
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
    ]

    with pytest.raises(ValueError, match="Invalid screenshot crop bounds"):
        await service.desktop.desktop_action(
            _workspace_id(service),
            "screenshot",
            {"crop_x": 1900, "crop_y": 0, "crop_w": 100, "crop_h": 100},
        )


def _last_shell_command(service: WorkspaceService) -> str:
    """Return the last desktop shell command executed by *service*."""
    call = _runtime(service).exec_command_wait.await_args_list[-1]
    return call.args[1][2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["bogus", "move", "click", "drag", "scroll", "type", "key", "open_url"]
)
async def test_unknown_action_raises(service: WorkspaceService, action: str) -> None:
    _runtime(service).exec_command_wait.return_value = (0, "alive")

    with pytest.raises(ValueError, match=f"Unknown desktop action: {action}"):
        await service.desktop.desktop_action(_workspace_id(service), action)
    # Only the shared liveness probe runs; no input command is executed.
    _runtime(service).exec_command_wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_screenshot_png_format(service: WorkspaceService) -> None:
    png = b"\x89PNG\r\n\x1a\n"
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(png).decode()),
    ]

    result = await service.desktop.desktop_action(
        _workspace_id(service), "screenshot", {"format": "png"}
    )

    assert result["ok"] is True
    assert result["mime"] == "image/png"
    assert result["image_b64"] == base64.b64encode(png).decode()
    assert result["width"] == 1920
    assert result["height"] == 1080
    command = _last_shell_command(service)
    assert "-vcodec png" in command
    assert "mjpeg" not in command


@pytest.mark.asyncio
async def test_screenshot_scale_reports_output_dimensions(
    service: WorkspaceService,
) -> None:
    png = b"\x89PNG\r\n\x1a\n"
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(png).decode()),
    ]

    result = await service.desktop.desktop_action(
        _workspace_id(service),
        "screenshot",
        {"format": "png", "max_dimension": 2400},
    )

    # 1920x1080 already fits in 2400: dimensions unchanged, no scale filter.
    assert result["width"] == 1920
    assert result["height"] == 1080
    command = _last_shell_command(service)
    assert "scale=" not in command


@pytest.mark.asyncio
async def test_screenshot_scale_down_proportionally(
    service: WorkspaceService,
) -> None:
    png = b"\x89PNG\r\n\x1a\n"
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "3840 2160"),
        (0, base64.b64encode(png).decode()),
    ]

    result = await service.desktop.desktop_action(
        _workspace_id(service),
        "screenshot",
        {"format": "png", "max_dimension": 2400},
    )

    assert result["width"] == 2400
    assert result["height"] == 1350
    command = _last_shell_command(service)
    assert "scale=2400:1350" in command


@pytest.mark.asyncio
async def test_screenshot_rejects_invalid_format(
    service: WorkspaceService,
) -> None:
    _runtime(service).exec_command_wait.side_effect = [(0, "alive"), (0, "1920 1080")]

    with pytest.raises(ValueError, match="Invalid screenshot format"):
        await service.desktop.desktop_action(
            _workspace_id(service), "screenshot", {"format": "webp"}
        )


@pytest.mark.asyncio
async def test_screenshot_rejects_invalid_max_dimension(
    service: WorkspaceService,
) -> None:
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, "alive"),
        (0, "1920 1080"),
    ]

    with pytest.raises(ValueError, match="Invalid screenshot max_dimension"):
        await service.desktop.desktop_action(
            _workspace_id(service), "screenshot", {"max_dimension": 0}
        )
    with pytest.raises(ValueError, match="Invalid screenshot max_dimension"):
        await service.desktop.desktop_action(
            _workspace_id(service), "screenshot", {"max_dimension": "huge"}
        )


@pytest.mark.asyncio
async def test_execute_success_runs_python_with_desktop_env(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute runs ``python3 -c <code>`` with HOME/DISPLAY after live probe."""
    _runtime(service).exec_command_wait.return_value = (0, "alive")
    calls: list[dict[str, Any]] = []

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        calls.append(
            {"command": command, "workdir": workdir, "env": env}
        )
        return 0, "out", "err"

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _fake_exec)

    result = await service.desktop.desktop_action(
        _workspace_id(service), "execute", {"code": "print(1)"}
    )

    assert result == {"ok": True, "exit_code": 0, "stdout": "out", "stderr": "err"}
    # Live probe + Xauthority self-heal, then the snippet itself.
    assert _runtime(service).exec_command_wait.await_count == 2
    assert calls[0]["command"] == ["python3", "-c", "print(1)"]
    assert calls[0]["env"] == {
        "HOME": "/root",
        "DISPLAY": ":1",
        "XAUTHORITY": "/root/.Xauthority",
    }


async def test_execute_self_heals_missing_xauthority(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute touches ``/root/.Xauthority`` so python-Xlib can connect.

    Regression: Xvnc uses ``-SecurityTypes None``, but python-Xlib
    unconditionally opens ``$XAUTHORITY``/``~/.Xauthority`` — without the
    file every PyAutoGUI snippet dies with ``FileNotFoundError`` before
    even connecting. The touch is idempotent and failures only warn
    (they degrade to the previous behaviour, never fail the action).
    """
    _runtime(service).exec_command_wait.return_value = (0, "alive")
    calls: list[dict[str, Any]] = []

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        calls.append(
            {"command": command, "workdir": workdir, "env": env}
        )
        return 0, "out", "err"

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _fake_exec)

    await service.desktop.desktop_action(
        _workspace_id(service), "execute", {"code": "print(1)"}
    )

    touch_calls = [
        call
        for call in _runtime(service).exec_command_wait.await_args_list
        if "touch /root/.Xauthority" in " ".join(str(a) for a in call.args[1])
    ]
    assert len(touch_calls) == 1
    assert touch_calls[0].kwargs["env"] == {
        "HOME": "/root",
        "DISPLAY": ":1",
        "XAUTHORITY": "/root/.Xauthority",
    }
    assert calls[0]["env"]["XAUTHORITY"] == "/root/.Xauthority"


async def test_execute_xauthority_heal_failure_still_runs_code(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing Xauthority touch must not fail the action itself."""
    _runtime(service).exec_command_wait.side_effect = [
        (0, "alive"),
        (1, "touch failed"),
    ]

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        return 0, "out", "err"

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _fake_exec)

    result = await service.desktop.desktop_action(
        _workspace_id(service), "execute", {"code": "print(1)"}
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0


async def test_desktop_env_pins_xauthority(
    service: WorkspaceService,
) -> None:
    """Every X11 client resolves auth via the runner-owned file."""
    assert service.desktop._desktop_env() == {
        "HOME": "/root",
        "DISPLAY": ":1",
        "XAUTHORITY": "/root/.Xauthority",
    }


@pytest.mark.asyncio
async def test_execute_rejects_empty_code(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/blank code is rejected before any probe or exec."""
    _runtime(service).exec_command_wait.return_value = (0, "alive")
    called = False

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return 0, "", ""

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _fake_exec)

    for code in ("", "   "):
        with pytest.raises(ValueError, match="code must not be empty"):
            await service.desktop.desktop_action(
                _workspace_id(service), "execute", {"code": code}
            )
    assert _runtime(service).exec_command_wait.await_count == 0
    assert called is False


@pytest.mark.asyncio
async def test_execute_rejects_oversized_code(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code above 200k chars is rejected before any probe or exec."""
    _runtime(service).exec_command_wait.return_value = (0, "alive")
    called = False

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return 0, "", ""

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _fake_exec)

    with pytest.raises(ValueError, match="code exceeds 200000 characters"):
        await service.desktop.desktop_action(
            _workspace_id(service), "execute", {"code": "x" * 200_001}
        )
    assert _runtime(service).exec_command_wait.await_count == 0
    assert called is False


@pytest.mark.asyncio
async def test_execute_returns_nonzero_output(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nonzero exits are reported (not raised) with stdout/stderr."""
    _runtime(service).exec_command_wait.return_value = (0, "alive")

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        return 3, "partial", "boom"

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _fake_exec)

    result = await service.desktop.desktop_action(
        _workspace_id(service), "execute", {"code": "print(1)"}
    )

    assert result == {
        "ok": True,
        "exit_code": 3,
        "stdout": "partial",
        "stderr": "boom",
    }


@pytest.mark.asyncio
async def test_execute_timeout_propagates(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hanging exec surfaces ``asyncio.TimeoutError`` to the caller."""
    import asyncio

    _runtime(service).exec_command_wait.return_value = (0, "alive")

    async def _hanging_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        raise asyncio.TimeoutError()

    monkeypatch.setattr(service.desktop, "_exec_harness_command", _hanging_exec)

    with pytest.raises(asyncio.TimeoutError):
        await service.desktop.desktop_action(
            _workspace_id(service), "execute", {"code": "print(1)"}
        )
