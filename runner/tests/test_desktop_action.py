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


def _last_shell_command(service: WorkspaceService) -> str:
    """Return the last desktop shell command executed by *service*."""
    call = service._runtime.exec_command_wait.await_args_list[-1]
    return call.args[1][2]


@pytest.mark.asyncio
async def test_key_enter_uses_xdotool_return(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    result = await service.desktop_action(
        service._workspace_id, "key", {"key": "enter", "modifiers": []}
    )

    assert result == {"ok": True}
    command = _last_shell_command(service)
    assert command == "xdotool key --clearmodifiers Return"
    assert "xdotool key -- " not in command


@pytest.mark.asyncio
async def test_key_enter_is_case_insensitive(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    await service.desktop_action(
        service._workspace_id, "key", {"key": "ENTER", "modifiers": []}
    )

    assert _last_shell_command(service) == "xdotool key --clearmodifiers Return"


@pytest.mark.asyncio
async def test_key_tab_and_escape_use_x11_keysyms(
    service: WorkspaceService,
) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, ""),
        (0, "alive"),
        (0, ""),
    ]

    await service.desktop_action(
        service._workspace_id, "key", {"key": "tab", "modifiers": []}
    )
    assert _last_shell_command(service) == "xdotool key --clearmodifiers Tab"

    await service.desktop_action(
        service._workspace_id, "key", {"key": "escape", "modifiers": []}
    )
    assert _last_shell_command(service) == "xdotool key --clearmodifiers Escape"


@pytest.mark.asyncio
async def test_key_control_c_uses_ctrl_modifier(
    service: WorkspaceService,
) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    await service.desktop_action(
        service._workspace_id,
        "key",
        {"key": "c", "modifiers": ["control"]},
    )

    assert _last_shell_command(service) == "xdotool key --clearmodifiers ctrl+c"


@pytest.mark.asyncio
async def test_key_command_maps_to_super(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    await service.desktop_action(
        service._workspace_id,
        "key",
        {"key": "a", "modifiers": ["command"]},
    )

    assert _last_shell_command(service) == "xdotool key --clearmodifiers super+a"


@pytest.mark.asyncio
async def test_key_combo_in_key_field_is_split(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    await service.desktop_action(
        service._workspace_id,
        "key",
        {"key": "ctrl+enter", "modifiers": []},
    )

    assert _last_shell_command(service) == "xdotool key --clearmodifiers ctrl+Return"


@pytest.mark.asyncio
async def test_key_unknown_name_with_exit_zero_raises(
    service: WorkspaceService,
) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "No such key name 'foo'. Ignoring it."),
    ]

    with pytest.raises(RuntimeError, match="Failed to send key"):
        await service.desktop_action(
            service._workspace_id, "key", {"key": "foo", "modifiers": []}
        )


@pytest.mark.asyncio
async def test_type_omits_end_of_options_dash(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    result = await service.desktop_action(
        service._workspace_id, "type", {"text": "hello"}
    )

    assert result == {"ok": True}
    command = _last_shell_command(service)
    assert command == "xdotool type --delay 0 --clearmodifiers hello"
    assert "xdotool type --delay 0 -- " not in command


@pytest.mark.asyncio
async def test_type_leading_dash_uses_file_stdin(
    service: WorkspaceService,
) -> None:
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "")]

    await service.desktop_action(
        service._workspace_id, "type", {"text": "-n flag"}
    )

    command = _last_shell_command(service)
    assert command == (
        "printf '%s' '-n flag' | "
        "xdotool type --delay 0 --clearmodifiers --file -"
    )


@pytest.mark.asyncio
async def test_unknown_action_raises(service: WorkspaceService) -> None:
    service._runtime.exec_command_wait.return_value = (0, "alive")

    with pytest.raises(ValueError, match="Unknown desktop action"):
        await service.desktop_action(service._workspace_id, "bogus")


@pytest.mark.asyncio
async def test_screenshot_png_format(service: WorkspaceService) -> None:
    png = b"\x89PNG\r\n\x1a\n"
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(png).decode()),
    ]

    result = await service.desktop_action(
        service._workspace_id, "screenshot", {"format": "png"}
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
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, base64.b64encode(png).decode()),
    ]

    result = await service.desktop_action(
        service._workspace_id,
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
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "3840 2160"),
        (0, base64.b64encode(png).decode()),
    ]

    result = await service.desktop_action(
        service._workspace_id,
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
    service._runtime.exec_command_wait.side_effect = [(0, "alive"), (0, "1920 1080")]

    with pytest.raises(ValueError, match="Invalid screenshot format"):
        await service.desktop_action(
            service._workspace_id, "screenshot", {"format": "webp"}
        )


@pytest.mark.asyncio
async def test_screenshot_rejects_invalid_max_dimension(
    service: WorkspaceService,
) -> None:
    service._runtime.exec_command_wait.side_effect = [
        (0, "alive"),
        (0, "1920 1080"),
        (0, "alive"),
        (0, "1920 1080"),
    ]

    with pytest.raises(ValueError, match="Invalid screenshot max_dimension"):
        await service.desktop_action(
            service._workspace_id, "screenshot", {"max_dimension": 0}
        )
    with pytest.raises(ValueError, match="Invalid screenshot max_dimension"):
        await service.desktop_action(
            service._workspace_id, "screenshot", {"max_dimension": "huge"}
        )


@pytest.mark.asyncio
async def test_execute_success_runs_python_with_desktop_env(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute runs ``python3 -c <code>`` with HOME/DISPLAY after live probe."""
    service._runtime.exec_command_wait.return_value = (0, "alive")
    calls: list[dict[str, Any]] = []

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        calls.append(
            {"command": command, "workdir": workdir, "env": env}
        )
        return 0, "out", "err"

    monkeypatch.setattr(service, "exec_harness_command", _fake_exec)

    result = await service.desktop_action(
        service._workspace_id, "execute", {"code": "print(1)"}
    )

    assert result == {"ok": True, "exit_code": 0, "stdout": "out", "stderr": "err"}
    assert service._runtime.exec_command_wait.await_count == 1
    assert calls[0]["command"] == ["python3", "-c", "print(1)"]
    assert calls[0]["env"] == {"HOME": "/root", "DISPLAY": ":1"}


@pytest.mark.asyncio
async def test_execute_rejects_empty_code(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/blank code is rejected before any probe or exec."""
    service._runtime.exec_command_wait.return_value = (0, "alive")
    called = False

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return 0, "", ""

    monkeypatch.setattr(service, "exec_harness_command", _fake_exec)

    for code in ("", "   "):
        with pytest.raises(ValueError, match="code must not be empty"):
            await service.desktop_action(
                service._workspace_id, "execute", {"code": code}
            )
    assert service._runtime.exec_command_wait.await_count == 0
    assert called is False


@pytest.mark.asyncio
async def test_execute_rejects_oversized_code(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code above 200k chars is rejected before any probe or exec."""
    service._runtime.exec_command_wait.return_value = (0, "alive")
    called = False

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return 0, "", ""

    monkeypatch.setattr(service, "exec_harness_command", _fake_exec)

    with pytest.raises(ValueError, match="code exceeds 200000 characters"):
        await service.desktop_action(
            service._workspace_id, "execute", {"code": "x" * 200_001}
        )
    assert service._runtime.exec_command_wait.await_count == 0
    assert called is False


@pytest.mark.asyncio
async def test_execute_returns_nonzero_output(
    service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nonzero exits are reported (not raised) with stdout/stderr."""
    service._runtime.exec_command_wait.return_value = (0, "alive")

    async def _fake_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        return 3, "partial", "boom"

    monkeypatch.setattr(service, "exec_harness_command", _fake_exec)

    result = await service.desktop_action(
        service._workspace_id, "execute", {"code": "print(1)"}
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

    service._runtime.exec_command_wait.return_value = (0, "alive")

    async def _hanging_exec(workspace_id, command, workdir="/workspace", env=None):  # type: ignore[no-untyped-def]
        raise asyncio.TimeoutError()

    monkeypatch.setattr(service, "exec_harness_command", _hanging_exec)

    with pytest.raises(asyncio.TimeoutError):
        await service.desktop_action(
            service._workspace_id, "execute", {"code": "print(1)"}
        )
