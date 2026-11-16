"""Guest OS descriptor: Linux stays unchanged, Windows spellings map to /workspace."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.harness.access.base import (
    WorkspaceAccessor,
    sanitize_exec_workdir,
    sanitize_harness_path,
)
from apps.harness.access.environment import ExecutionEnvironmentAccessor
from apps.harness.access.guest import (
    LINUX_GUEST,
    WINDOWS_GUEST,
    guest_for_os,
    guest_of,
)
from apps.harness.access.runner_accessor import create_harness_accessor


@pytest.mark.parametrize(
    "raw",
    ["/workspace/a.py", "src/a.py", "C:\\workspace\\a.py", "/c/workspace", "a\\b"],
)
def test_linux_guest_keeps_every_path_unchanged(raw: str) -> None:
    assert LINUX_GUEST.to_workspace_path(raw) == raw
    assert LINUX_GUEST.to_native_path(raw) == raw


def test_linux_guest_reproduces_todays_values() -> None:
    assert LINUX_GUEST.name == "linux"
    assert LINUX_GUEST.display_name == "Linux"
    assert LINUX_GUEST.native_python == "/usr/bin/python3"
    assert LINUX_GUEST.native_shell == "/bin/sh"
    assert LINUX_GUEST.case_sensitive_paths is True
    assert (
        LINUX_GUEST.artifact_executable("/opt/opencuria/runtimes/claude-agent/1", "claude")
        == "/opt/opencuria/runtimes/claude-agent/1/claude"
    )
    assert LINUX_GUEST.desktop_environment(
        {"display": ":1", "xauthority": "/root/.Xauthority"}
    ) == {"DISPLAY": ":1", "XAUTHORITY": "/root/.Xauthority"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\workspace\\a.py", "/workspace/a.py"),
        ("c:/Workspace/src/a.py", "/workspace/src/a.py"),
        ("C:\\WORKSPACE", "/workspace"),
        ("C:\\workspace\\", "/workspace"),
        ("/c/workspace/a.py", "/workspace/a.py"),
        ("/C/workspace", "/workspace"),
        ("C:/workspace/.opencuria\\config\\projects\\x.jsonl",
         "/workspace/.opencuria/config/projects/x.jsonl"),
        ("C:\\\\workspace\\\\a", "/workspace/a"),
        ("C:\\workspace\\sub\\..\\a.py", "/workspace/a.py"),
        ("/workspace/a\\b", "/workspace/a/b"),
        ("src\\a.py", "src/a.py"),
        ("/etc/hosts", "/etc/hosts"),
    ],
)
def test_windows_guest_maps_native_spellings(raw: str, expected: str) -> None:
    assert WINDOWS_GUEST.to_workspace_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "C:\\Users\\root\\.ssh\\id_rsa",
        "D:\\workspace\\a.py",
        "C:\\workspace\\..\\Windows\\System32",
        "C:\\workspacefoo\\a",
        "C:workspace\\a",
        "\\\\server\\share\\a",
        "//server/share/a",
        "\\\\?\\C:\\workspace\\a",
        "/c/Users/root",
        "/d/workspace",
        "C:\\wor\u212aspace\\a",
    ],
)
def test_windows_guest_rejects_paths_outside_the_workspace(raw: str) -> None:
    assert WINDOWS_GUEST.to_workspace_path(raw) is None


def test_windows_guest_native_paths_and_values() -> None:
    assert WINDOWS_GUEST.to_native_path("/workspace") == "C:\\workspace"
    assert WINDOWS_GUEST.to_native_path("/workspace/a/b/") == "C:\\workspace\\a\\b"
    with pytest.raises(ValueError, match="outside"):
        WINDOWS_GUEST.to_native_path("/tmp/x")
    assert WINDOWS_GUEST.display_name == "Windows 11"
    assert WINDOWS_GUEST.native_python == "C:\\msys64\\usr\\bin\\python3.exe"
    assert WINDOWS_GUEST.native_shell == "C:\\msys64\\usr\\bin\\sh.exe"
    assert WINDOWS_GUEST.case_sensitive_paths is False
    assert WINDOWS_GUEST.artifact_executable("/opt/x", "claude") == "/opt/x/claude.exe"
    assert WINDOWS_GUEST.desktop_environment({"ok": True}) == {}


def test_guest_selection_defaults_to_linux() -> None:
    assert guest_for_os("windows") is WINDOWS_GUEST
    assert guest_for_os("linux") is LINUX_GUEST
    assert guest_for_os(None) is LINUX_GUEST
    assert guest_for_os("beos") is LINUX_GUEST
    assert guest_of(MagicMock()) is LINUX_GUEST
    assert guest_of(object()) is LINUX_GUEST
    assert WorkspaceAccessor.guest is LINUX_GUEST


def test_execution_environment_passes_the_guest_through() -> None:
    inner = MagicMock()
    inner.guest = WINDOWS_GUEST
    assert ExecutionEnvironmentAccessor(inner, {}).guest is WINDOWS_GUEST
    assert ExecutionEnvironmentAccessor(object(), {}).guest is LINUX_GUEST


def test_path_sanitizers_keep_linux_rules() -> None:
    assert sanitize_harness_path("C:\\x") == "/workspace/C:\\x"
    assert sanitize_harness_path("a/../b") == "/workspace/b"
    assert sanitize_exec_workdir("/tmp") == "/tmp"


def test_path_sanitizers_map_windows_spellings() -> None:
    assert sanitize_harness_path("C:\\workspace\\a.py", WINDOWS_GUEST) == (
        "/workspace/a.py"
    )
    assert sanitize_harness_path("src\\..\\a.py", WINDOWS_GUEST) == "/workspace/a.py"
    assert sanitize_exec_workdir("/c/workspace/app", WINDOWS_GUEST) == (
        "/workspace/app"
    )
    assert sanitize_exec_workdir("", WINDOWS_GUEST) == "/workspace"


@pytest.mark.parametrize("raw", ["C:\\Users\\root\\x", "\\\\server\\share\\x"])
def test_path_sanitizers_reject_windows_paths_outside_the_workspace(raw: str) -> None:
    with pytest.raises(ValueError, match="outside /workspace"):
        sanitize_harness_path(raw, WINDOWS_GUEST)
    with pytest.raises(ValueError, match="Invalid workdir"):
        sanitize_exec_workdir(raw, WINDOWS_GUEST)


class _Service:
    """Runner service stub; these tests never emit."""

    runner_transport = None
    stream_transport = None


@pytest.mark.django_db(transaction=True)
async def test_create_harness_accessor_serves_the_image_guest(harness_workspace) -> None:
    from asgiref.sync import sync_to_async

    from apps.runners.models import ImageInstance

    accessor = await create_harness_accessor(_Service(), str(harness_workspace.id))
    assert accessor.guest is LINUX_GUEST

    def attach_windows_image() -> None:
        image = ImageInstance.objects.create(
            is_legacy=True,
            runner=harness_workspace.runner,
            origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
            name="Windows 11",
            runner_ref="windows-11",
            status=ImageInstance.Status.READY,
            guest_os="windows",
        )
        harness_workspace.base_image_instance = image
        harness_workspace.save(update_fields=["base_image_instance"])

    await sync_to_async(attach_windows_image)()
    accessor = await create_harness_accessor(_Service(), str(harness_workspace.id))
    assert accessor.guest is WINDOWS_GUEST
