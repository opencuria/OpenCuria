"""Codex on Windows guests: native paths out, workspace paths back in."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

import pytest

from apps.harness.access.guest import LINUX_GUEST, WINDOWS_GUEST
from apps.harness.engines.base import RunOptions
from apps.harness.engines.codex import engine as codex_engine
from apps.harness.engines.codex.configuration import runtime_settings
from apps.harness.engines.codex.engine import CodexEngine
from apps.harness.engines.codex.events import CodexEventProjector, workspace_paths
from apps.harness.engines.codex.mcp_bridge import ManagedMcpBridge
from apps.harness.engines.codex.media import image_attachments
from apps.harness.tests.conftest import TINY_PNG, FakeAccessor
from apps.harness.tests.test_codex_core_safety import auth
from apps.harness.tools import default_tool_registry

CODEX_DIR = "/opt/opencuria/runtimes/codex-agent/0.166.0/bin"


class _StopError(Exception):
    """Ends the fake launch once the turn would start."""


class _LaunchAccessor:
    """Runner seam of one Codex launch; records what reaches the guest."""

    def __init__(self, guest: Any, binary: str) -> None:
        self.workspace_id = str(uuid.uuid4())
        self.guest = guest
        self.binary = binary
        self.opened: list[dict[str, Any]] = []

    async def ensure_runtime_artifact(self, artifact_id: str, version: str) -> dict:
        return {"ok": True, "path": self.binary, "version": version}

    async def open_process(self, argv, workdir, env, **kwargs):  # type: ignore[no-untyped-def]
        self.opened.append({"argv": argv, "workdir": workdir, "env": env})
        return object()


class _Lease:
    def __init__(self, accessor: Any, **kwargs: Any) -> None:
        self.owner_id = kwargs.get("owner_id", "")
        self.owner = {"lease_id": "lease"}
        self.healthy = True

    async def reserve(self) -> dict[str, Any]:
        return {}

    async def aclose(self) -> None:
        return None


def _patch_launch(monkeypatch) -> tuple[list[tuple[str, dict]], list[str]]:  # type: ignore[no-untyped-def]
    requests: list[tuple[str, dict]] = []
    configs: list[str] = []

    class Protocol:
        def __init__(self, stream: Any, handler: Any) -> None:
            self.failure = None

        async def request(self, method: str, params: dict, timeout=None):  # type: ignore[no-untyped-def]
            requests.append((method, params))
            if method == "account/login/start":
                return {"type": "chatgptAuthTokens"}
            if method == "thread/start":
                return {"thread": {"id": str(uuid.uuid4())}}
            if method == "turn/start":
                raise _StopError
            return {}

        async def send(self, message: dict) -> None:
            return None

        async def close(self) -> None:
            return None

    async def state_action(accessor, action, home, *args):  # type: ignore[no-untyped-def]
        if action == "config":
            configs.append(base64.b64decode(args[0]).decode())

    async def materialize_skills(*_args: Any) -> None:
        return None

    monkeypatch.setattr(codex_engine, "CodexProtocol", Protocol)
    monkeypatch.setattr(codex_engine, "DesktopLease", _Lease)
    monkeypatch.setattr(codex_engine, "state_action", state_action)
    monkeypatch.setattr(codex_engine, "materialize_skills", materialize_skills)
    return requests, configs


async def _launch(accessor: _LaunchAccessor) -> None:
    engine = CodexEngine(
        tools=default_tool_registry(accessor.guest),
        accessor=accessor,  # type: ignore[arg-type]
        auth=auth(),
    )
    await engine.run(
        "Local test only.",
        "build",
        "chatgpt/gpt-6.1-sol",
        opts=RunOptions(session_id=str(uuid.uuid4())),
    )


async def test_windows_launch_sends_native_cwd_trust_and_roots(monkeypatch) -> None:
    requests, configs = _patch_launch(monkeypatch)
    accessor = _LaunchAccessor(WINDOWS_GUEST, f"{CODEX_DIR}/codex-app-server.exe")
    with pytest.raises(_StopError):
        await _launch(accessor)
    params = dict(requests)
    assert params["thread/start"]["cwd"] == "C:\\workspace"
    assert params["thread/start"]["runtimeWorkspaceRoots"] == ["C:\\workspace"]
    assert params["thread/start"]["config"]["projects"] == {
        "C:\\workspace": {"trust_level": "trusted"}
    }
    assert params["turn/start"]["cwd"] == "C:\\workspace"
    assert 'projects = { "C:\\\\workspace" = ' in configs[0]
    assert "Windows 11 VM" in params["thread/start"]["developerInstructions"]
    # The runner still starts the process from the POSIX workspace.
    assert accessor.opened[0]["workdir"] == "/workspace"
    assert accessor.opened[0]["argv"][0] == f"{CODEX_DIR}/codex-app-server.exe"


async def test_linux_launch_keeps_posix_cwd(monkeypatch) -> None:
    requests, _configs = _patch_launch(monkeypatch)
    with pytest.raises(_StopError):
        await _launch(_LaunchAccessor(LINUX_GUEST, f"{CODEX_DIR}/codex-app-server"))
    params = dict(requests)
    assert params["thread/start"]["cwd"] == "/workspace"
    assert params["thread/start"]["runtimeWorkspaceRoots"] == ["/workspace"]
    assert params["thread/start"]["config"]["projects"] == {
        "/workspace": {"trust_level": "trusted"}
    }


@pytest.mark.parametrize(
    "binary", [f"{CODEX_DIR}/codex-app-server", "C:\\codex\\codex-app-server.exe"]
)
async def test_windows_launch_requires_the_exe_entrypoint(monkeypatch, binary) -> None:
    requests, _configs = _patch_launch(monkeypatch)
    accessor = _LaunchAccessor(WINDOWS_GUEST, binary)
    with pytest.raises(ValueError, match="invalid binary path"):
        await _launch(accessor)
    assert requests == [] and accessor.opened == []


def test_openrouter_auth_command_uses_the_guest_shell() -> None:
    linux, _, _ = runtime_settings(
        auth("openrouter"), "openrouter/openai/gpt-5", readonly=False
    )
    windows, _, _ = runtime_settings(
        auth("openrouter"),
        "openrouter/openai/gpt-5",
        readonly=False,
        native_shell=WINDOWS_GUEST.native_shell,
    )
    linux_auth = linux["model_providers.openrouter"]["auth"]
    windows_auth = windows["model_providers.openrouter"]["auth"]
    assert linux_auth["command"] == "/bin/sh"
    assert windows_auth["command"] == "C:\\msys64\\usr\\bin\\sh.exe"
    assert windows_auth["args"] == linux_auth["args"]


class _RelayStream:
    def __init__(self) -> None:
        self.ready = [b'{"ready": "/tmp/opencuria-mcp-1/rpc"}\n']
        self.closed = asyncio.Event()

    async def receive(self) -> bytes:
        if self.ready:
            return self.ready.pop()
        await self.closed.wait()
        return b""

    async def send(self, data: bytes) -> None:
        return None

    async def send_eof(self) -> None:
        self.closed.set()

    async def wait_closed(self) -> int:
        return 0

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.parametrize(
    ("guest", "client"),
    [
        (LINUX_GUEST, "/usr/bin/python3"),
        (WINDOWS_GUEST, "C:\\msys64\\usr\\bin\\python3.exe"),
    ],
)
async def test_managed_mcp_relay_client_uses_the_guest_python(guest, client) -> None:
    opened: list[list[str]] = []

    class Accessor:
        def __init__(self) -> None:
            self.guest = guest

        async def open_process(self, argv, cwd, env, **kwargs):  # type: ignore[no-untyped-def]
            opened.append(argv)
            return _RelayStream()

    bridge = ManagedMcpBridge([{"name": "js", "inputSchema": {}}], lambda _: None)
    config = await bridge.start(Accessor(), "/workspace", {"lease_id": "x"})  # type: ignore[arg-type]
    try:
        assert config["command"] == client
        assert config["args"][-1] == "/tmp/opencuria-mcp-1/rpc"
        # The relay server itself always runs under the guest's POSIX bash.
        assert opened[0][0] == "/usr/bin/python3"
    finally:
        await bridge.close()


def test_workspace_paths_maps_native_codex_items() -> None:
    item = {
        "type": "commandExecution",
        "cwd": "C:\\workspace",
        "commandActions": [{"type": "read", "path": "C:\\workspace\\a.py"}],
        "changes": [
            {"path": "c:/workspace/b.py", "diff": ""},
            {"path": "D:\\other\\c.py"},
        ],
        "savedPath": "C:\\workspace\\img.png",
    }
    mapped = workspace_paths(item, WINDOWS_GUEST.to_workspace_path)
    assert mapped["cwd"] == "/workspace"
    assert mapped["commandActions"][0]["path"] == "/workspace/a.py"
    assert [change["path"] for change in mapped["changes"]] == [
        "/workspace/b.py",
        "D:\\other\\c.py",
    ]
    assert mapped["savedPath"] == "/workspace/img.png"
    assert workspace_paths(item, LINUX_GUEST.to_workspace_path) == item


async def test_projector_reports_workspace_paths_for_windows_items() -> None:
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any], parent: str | None = None) -> None:
        events.append(event)

    async def no_images(_item: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    projector = CodexEventProjector(
        emit=emit,
        model=lambda: "chatgpt/gpt-6.1-sol",
        is_managed_tool=lambda _name: False,
        image_attachments=no_images,
        to_workspace_path=WINDOWS_GUEST.to_workspace_path,
    )
    memory = "C:\\workspace\\.opencuria\\harness\\codex\\x\\config\\memories\\m.md"
    await projector.item(
        {
            "type": "fileChange",
            "id": "edit-1",
            "status": "completed",
            "changes": [{"path": "C:\\workspace\\src\\a.py", "diff": "+x"}],
        },
        completed=True,
    )
    await projector.item(
        {
            "type": "fileChange",
            "id": "edit-2",
            "status": "completed",
            "changes": [{"path": memory, "diff": "+m"}],
        },
        completed=True,
    )
    patches = [event for event in events if event["type"] == "patch"]
    assert patches[0]["path"] == "/workspace/src/a.py"
    started = [event for event in events if event["type"] == "tool_started"]
    assert [event["tool"] for event in started] == ["edit", "memory"]
    assert json.loads(started[0]["arguments"])["changes"][0]["path"] == (
        "/workspace/src/a.py"
    )


async def test_image_attachments_read_windows_paths_inside_the_workspace() -> None:
    accessor = FakeAccessor(files={"/workspace/shot.png": TINY_PNG})
    accessor.guest = WINDOWS_GUEST
    authorized: list[str] = []

    async def authorize(path: str) -> bool:
        authorized.append(path)
        return True

    inside = await image_attachments(
        {"type": "imageView", "path": "C:\\workspace\\shot.png"},
        accessor=accessor,
        authorize_read=authorize,
    )
    assert [item["filename"] for item in inside] == ["shot.png"]
    outside = await image_attachments(
        {"type": "imageView", "path": "C:\\Users\\root\\shot.png"},
        accessor=accessor,
        authorize_read=authorize,
    )
    assert outside == []
    assert authorized == ["/workspace/shot.png"]
