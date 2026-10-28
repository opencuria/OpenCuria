"""Regular downloads: real guest commands, bounded output and RPC contracts."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.services import files as files_module
from src.services.files import FileManager

from tests.directory_download_runtime import LocalFileRuntime


@pytest.fixture
def directory() -> Iterator[Path]:
    """Use real containment checks with disposable files inside /workspace."""
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="file-download-test-", dir=repo) as path:
        yield Path(path)


def _manager(runtime: LocalFileRuntime) -> tuple[FileManager, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(workspace_id, "local", "running")
    return FileManager(
        get_cached=lambda _: info, get_runtime=lambda _: runtime
    ), workspace_id


@pytest.mark.parametrize(
    "content",
    [b"", b"hello\n", bytes(range(256)) * 1024],
    ids=["empty", "text", "binary"],
)
@pytest.mark.parametrize("name", ["file.bin", "--help", "file ' ; echo INJECTED ; $x"])
async def test_regular_download_uses_two_guest_calls(
    directory: Path,
    content: bytes,
    name: str,
) -> None:
    source = directory / name
    source.write_bytes(content)
    runtime = LocalFileRuntime()
    manager, workspace_id = _manager(runtime)

    payload = await manager.download_file(workspace_id, str(source))

    assert payload == {
        "content": base64.encodebytes(content).decode().strip(),
        "filename": name,
        "is_archive": False,
        "size": len(content),
    }
    assert len(runtime.calls) == 2  # Containment, then type/size/content.
    assert runtime.calls[0] == ["realpath", "-m", str(source)]


async def test_exact_cap_allowed_and_oversize_rejected_before_base64(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = directory / "payload.bin"
    source.write_bytes(b"abcd")
    tools = directory / "bin"
    tools.mkdir()
    marker = directory / "base64-called"
    executable = tools / "base64"
    executable.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
        f'exec {shlex.quote(str(shutil.which("base64")))} "$@"\n'
    )
    executable.chmod(0o755)
    runtime = LocalFileRuntime({"PATH": str(tools) + os.pathsep + os.environ["PATH"]})
    manager, workspace_id = _manager(runtime)
    monkeypatch.setattr(files_module, "FILE_DOWNLOAD_MAX_SIZE", 4)

    assert (await manager.download_file(workspace_id, str(source)))["size"] == 4
    assert marker.exists()
    marker.unlink()
    source.write_bytes(b"abcde")

    with pytest.raises(ValueError, match="maximum size of 4 bytes"):
        await manager.download_file(workspace_id, str(source))
    assert runtime.output_bytes[-1] == 0
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["missing", "fifo"])
async def test_non_regular_file_fails_without_reading(
    directory: Path, kind: str
) -> None:
    source = directory / kind
    if kind == "fifo":
        os.mkfifo(source)
    manager, workspace_id = _manager(LocalFileRuntime())
    with pytest.raises(RuntimeError, match="Failed to download"):
        await asyncio.wait_for(manager.download_file(workspace_id, str(source)), 2)


async def test_symlinks_resolve_inside_and_reject_escape(
    directory: Path,
    tmp_path: Path,
) -> None:
    target = directory / "target.bin"
    target.write_bytes(b"inside")
    link = directory / "link"
    link.symlink_to(target)
    runtime = LocalFileRuntime()
    manager, workspace_id = _manager(runtime)
    result = await manager.download_file(workspace_id, str(link))
    assert result["filename"] == "target.bin"
    assert base64.b64decode(result["content"]) == b"inside"

    external = tmp_path / "secret"
    external.write_bytes(b"outside")
    link.unlink()
    link.symlink_to(external)
    runtime.calls.clear()
    with pytest.raises(ValueError, match="escapes /workspace"):
        await manager.download_file(workspace_id, str(link))
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("output", ["nonsense\n", "-1\nQUJD", "4", ""])
async def test_malformed_file_header_fails(output: str) -> None:
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [(0, "/workspace/file\n"), (0, output)]
    manager, workspace_id = _manager(runtime)
    with pytest.raises(RuntimeError, match="invalid size"):
        await manager.download_file(workspace_id, "/workspace/file")


@pytest.mark.parametrize("command", ["stat", "base64"])
async def test_guest_command_failure_is_not_a_successful_download(
    directory: Path,
    command: str,
) -> None:
    source = directory / "payload"
    source.write_bytes(b"data")
    tools = directory / "bin"
    tools.mkdir()
    executable = tools / command
    executable.write_text("#!/bin/sh\necho failed >&2\nexit 1\n")
    executable.chmod(0o755)
    manager, workspace_id = _manager(
        LocalFileRuntime({"PATH": str(tools) + os.pathsep + os.environ["PATH"]})
    )
    with pytest.raises(RuntimeError, match="Failed to download"):
        await manager.download_file(workspace_id, str(source))


async def test_concurrent_downloads_are_independent(directory: Path) -> None:
    paths = [directory / f"payload-{index}" for index in range(8)]
    for index, path in enumerate(paths):
        path.write_bytes(bytes([index]) * (64 * 1024))
    runtime = LocalFileRuntime()
    manager, workspace_id = _manager(runtime)
    payloads = await asyncio.gather(
        *(manager.download_file(workspace_id, str(path)) for path in paths)
    )
    for index, payload in enumerate(payloads):
        assert base64.b64decode(payload["content"]) == paths[index].read_bytes()
        assert payload["filename"] == paths[index].name
    assert len(runtime.calls) == 16


async def test_cancellation_preserves_guest_completion_contract(
    directory: Path,
) -> None:
    # Production exec cancellation stops waiting; it does not kill the guest
    # command. Let the real guest command finish and check for leaked resources.
    source = directory / "payload"
    source.write_bytes(b"data")
    ready, release = directory / "ready", directory / "release"
    tools = directory / "bin"
    tools.mkdir()
    executable = tools / "base64"
    executable.write_text(
        '#!/bin/sh\ntouch "$READY"\n'
        'while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n'
        f'exec {shlex.quote(str(shutil.which("base64")))} "$@"\n'
    )
    executable.chmod(0o755)
    runtime = LocalFileRuntime(
        {
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "READY": str(ready),
            "RELEASE": str(release),
        }
    )
    manager, workspace_id = _manager(runtime)
    task = asyncio.create_task(manager.download_file(workspace_id, str(source)))

    async def wait_ready() -> None:
        while not ready.exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_ready(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.touch()
        await asyncio.wait_for(
            asyncio.gather(*(process.communicate() for process in runtime.processes)),
            timeout=5,
        )
        if not task.done():
            await task
    assert all(process.returncode == 0 for process in runtime.processes)


@pytest.mark.parametrize("oversized", [False, True])
async def test_file_download_rpc_contract(
    directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized: bool,
) -> None:
    source = directory / "payload.bin"
    source.write_bytes(bytes(range(256)) * 2048)
    if oversized:
        monkeypatch.setattr(files_module, "FILE_DOWNLOAD_MAX_SIZE", 1024)
    manager, workspace_id = _manager(LocalFileRuntime())
    interface = WebSocketInterface(
        SimpleNamespace(files=manager),
        RunnerSettings(state_dir=str(tmp_path / "state"), _env_file=None),
    )
    interface._sio.emit = AsyncMock()
    await interface._sio.handlers["/"]["files:download"](
        {
            "workspace_id": str(workspace_id),
            "request_id": "download",
            "path": str(source),
        }
    )
    events = [call.args for call in interface._sio.emit.await_args_list]
    assert events[-1][0] == "files:download_result"
    result = events[-1][1]
    assert result["workspace_id"] == str(workspace_id)
    assert result["request_id"] == "download"
    if oversized:
        assert len(events) == 1
        assert "maximum size" in result["error"]
        assert result["content"] == ""
        assert len(json.dumps(result)) < 1024
    else:
        chunks = events[:-1]
        assert result["chunked"] is True
        assert all(event == "files:download_chunk" for event, _ in chunks)
        assert [item["index"] for _, item in chunks] == list(range(len(chunks)))
        assert all(item["total_chunks"] == len(chunks) for _, item in chunks)
        assert result["total_chunks"] == len(chunks)
        assert (
            base64.b64decode("".join(item["content"] for _, item in chunks))
            == source.read_bytes()
        )
        assert result["filename"] == source.name
        assert result["is_archive"] is False
        assert result["size"] == source.stat().st_size
