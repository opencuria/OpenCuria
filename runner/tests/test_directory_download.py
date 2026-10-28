"""Real guest commands: one archive pass, bounded output and safe cleanup."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shlex
import shutil
import tarfile
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
from src.services.directory_archive import directory_archive_command
from src.services.files import FileManager

from tests.directory_download_runtime import LocalFileRuntime


@pytest.fixture
def directory() -> Iterator[Path]:
    """Keep fixtures within the service's real /workspace containment boundary."""
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="download-test-", dir=repo) as path:
        source = Path(path) / "project"
        source.mkdir()
        (source / "hello.txt").write_bytes(b"hello\n")
        (source / "empty").mkdir()
        yield source


@pytest.fixture
def runtime(tmp_path: Path) -> LocalFileRuntime:
    """Count actual tar launches and give each test its own guest temp area."""
    spool = tmp_path / "spool"
    spool.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    tar = tools / "tar"
    real_tar = shutil.which("tar")
    assert real_tar is not None
    tar.write_text(
        '#!/bin/sh\nprintf "tar\\n" >> "$TAR_CALLS"\n'
        f'exec {shlex.quote(real_tar)} "$@"\n',
        encoding="utf-8",
    )
    tar.chmod(0o755)
    return LocalFileRuntime(
        {
            "TMPDIR": str(spool),
            "TAR_CALLS": str(tmp_path / "tar-calls"),
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        }
    )


def _manager(runtime: LocalFileRuntime) -> tuple[FileManager, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(workspace_id, "local", "running")
    return FileManager(
        get_cached=lambda _: info, get_runtime=lambda _: runtime
    ), workspace_id


def _assert_archive(payload: dict, name: str = "project") -> None:
    data = base64.b64decode(payload["content"])
    assert payload["size"] == len(data)
    assert payload["filename"] == name + ".tar.gz"
    assert payload["is_archive"] is True
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        assert archive.getmember(name).isdir()
        assert archive.getmember(name + "/empty").isdir()
        stream = archive.extractfile(name + "/hello.txt")
        assert stream is not None
        assert stream.read() == b"hello\n"


async def test_directory_download_compresses_once(
    directory: Path,
    runtime: LocalFileRuntime,
) -> None:
    manager, workspace_id = _manager(runtime)
    payload = await manager.download_file(workspace_id, str(directory))

    _assert_archive(payload)
    assert Path(runtime.env["TAR_CALLS"]).read_text() == "tar\n"
    assert len(runtime.calls) == 3  # Containment, directory probe, archive.
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


async def test_archive_preserves_permissions_and_symlinks(
    directory: Path,
    runtime: LocalFileRuntime,
) -> None:
    (directory / "hello.txt").chmod(0o751)
    (directory / "link").symlink_to("hello.txt")
    manager, workspace_id = _manager(runtime)
    payload = await manager.download_file(workspace_id, str(directory))

    with tarfile.open(
        fileobj=io.BytesIO(base64.b64decode(payload["content"]))
    ) as archive:
        member = archive.getmember("project/hello.txt")
        assert member.mode == 0o751
        assert member.uid == (directory / "hello.txt").stat().st_uid
        link = archive.getmember("project/link")
        assert link.issym()
        assert link.linkname == "hello.txt"


async def test_directory_exact_compressed_cap_is_allowed(
    directory: Path,
    runtime: LocalFileRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, workspace_id = _manager(runtime)
    first = await manager.download_file(workspace_id, str(directory))
    monkeypatch.setattr(files_module, "FILE_DOWNLOAD_MAX_SIZE", first["size"])

    _assert_archive(await manager.download_file(workspace_id, str(directory)))

    monkeypatch.setattr(files_module, "FILE_DOWNLOAD_MAX_SIZE", first["size"] - 1)
    with pytest.raises(ValueError, match="maximum size"):
        await manager.download_file(workspace_id, str(directory))
    assert runtime.output_bytes[-1] == 0
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


async def test_large_archive_is_rejected_without_payload_or_unbounded_spool(
    directory: Path,
    runtime: LocalFileRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This compressor attempts to write far beyond the cap. The kernel limits
    # its output file to one 512-byte block beyond the cap, with no payload.
    tar = Path(runtime.env["PATH"].split(os.pathsep)[0]) / "tar"
    tar.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        'try:\n    sys.stdout.buffer.write(b"x" * (1024 * 1024))\n'
        'finally:\n    open(os.environ["TAR_CALLS"], "w").write('
        "str(os.fstat(1).st_size))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(files_module, "FILE_DOWNLOAD_MAX_SIZE", 1024)
    manager, workspace_id = _manager(runtime)

    with pytest.raises(ValueError, match="maximum size"):
        await manager.download_file(workspace_id, str(directory))

    assert Path(runtime.env["TAR_CALLS"]).read_text() == "1536"
    assert runtime.output_bytes[-1] == 0
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


@pytest.mark.parametrize("name", ["project ' ; echo INJECTED ; $x", "--help"])
async def test_directory_names_are_opaque_argv(
    directory: Path,
    runtime: LocalFileRuntime,
    name: str,
) -> None:
    renamed = directory.rename(directory.with_name(name))
    manager, workspace_id = _manager(runtime)

    _assert_archive(await manager.download_file(workspace_id, str(renamed)), name)


async def test_archive_failure_discards_partial_output(
    directory: Path,
    runtime: LocalFileRuntime,
) -> None:
    tar = Path(runtime.env["PATH"].split(os.pathsep)[0]) / "tar"
    tar.write_text('#!/bin/sh\nprintf "partial archive"\nexit 2\n')
    manager, workspace_id = _manager(runtime)

    with pytest.raises(RuntimeError, match="Failed to download"):
        await manager.download_file(workspace_id, str(directory))

    assert runtime.output_bytes[-1] == len("Archive creation failed\n")
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


async def test_concurrent_downloads_have_independent_archives(
    directory: Path,
    runtime: LocalFileRuntime,
) -> None:
    manager, workspace_id = _manager(runtime)
    results = await asyncio.gather(
        *(manager.download_file(workspace_id, str(directory)) for _ in range(4))
    )

    for payload in results:
        _assert_archive(payload)
    assert Path(runtime.env["TAR_CALLS"]).read_text().splitlines() == ["tar"] * 4
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


async def test_caller_cancellation_leaves_no_named_archive(
    directory: Path,
    runtime: LocalFileRuntime,
    tmp_path: Path,
) -> None:
    # exec_command_wait cancellation does not kill guest commands in QEMU.
    # Let the guest finish after cancellation, matching that existing contract.
    ready, release = tmp_path / "ready", tmp_path / "release"
    tar = Path(runtime.env["PATH"].split(os.pathsep)[0]) / "tar"
    real_tar = shutil.which("tar")
    tar.write_text(
        '#!/bin/sh\nprintf "ready" > "$READY"\n'
        'while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n'
        f'exec {shlex.quote(str(real_tar))} "$@"\n',
        encoding="utf-8",
    )
    runtime.env.update(READY=str(ready), RELEASE=str(release))
    manager, workspace_id = _manager(runtime)
    task = asyncio.create_task(manager.download_file(workspace_id, str(directory)))

    async def wait_ready() -> None:
        while not ready.exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_ready(), timeout=5)
        assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []
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
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


async def test_directory_download_rejects_symlink_escape(
    directory: Path,
    runtime: LocalFileRuntime,
    tmp_path: Path,
) -> None:
    escape = directory / "escape"
    escape.symlink_to(tmp_path, target_is_directory=True)
    manager, workspace_id = _manager(runtime)

    with pytest.raises(ValueError, match="escapes /workspace"):
        await manager.download_file(workspace_id, str(escape))
    assert not Path(runtime.env["TAR_CALLS"]).exists()


@pytest.mark.parametrize("output", ["nonsense\n", "-1\nQUJD", "4", ""])
async def test_invalid_archive_header_is_an_error(output: str) -> None:
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [
        (0, "/workspace/project\n"),
        (0, "directory\n"),
        (0, output),
    ]
    manager, workspace_id = _manager(runtime)
    with pytest.raises(RuntimeError, match="invalid size"):
        await manager.download_file(workspace_id, "/workspace/project")


async def test_missing_directory_fails_before_emitting_archive(
    directory: Path,
    runtime: LocalFileRuntime,
) -> None:
    command = directory_archive_command(str(directory / "missing"), 1024)
    code, output = await runtime.exec_command_wait("local", command)

    assert code != 0
    assert output == "Archive creation failed\n"
    assert list(Path(runtime.env["TMPDIR"]).iterdir()) == []


@pytest.mark.parametrize("oversized", [False, True])
async def test_directory_download_rpc_contract(
    directory: Path,
    runtime: LocalFileRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized: bool,
) -> None:
    """The real archive reaches ordered chunks or a small final-only error."""
    (directory / "asset.bin").write_bytes(os.urandom(512 * 1024))
    if oversized:
        monkeypatch.setattr(files_module, "FILE_DOWNLOAD_MAX_SIZE", 1024)
    manager, workspace_id = _manager(runtime)
    interface = WebSocketInterface(
        SimpleNamespace(files=manager),
        RunnerSettings(state_dir=str(tmp_path / "state"), _env_file=None),
    )
    interface._sio.emit = AsyncMock()

    await interface._sio.handlers["/"]["files:download"](
        {
            "workspace_id": str(workspace_id),
            "request_id": "download",
            "path": str(directory),
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
        assert result["chunked"] is True
        chunks = events[:-1]
        assert all(event == "files:download_chunk" for event, _ in chunks)
        assert [item["index"] for _, item in chunks] == list(range(len(chunks)))
        assert all(item["total_chunks"] == len(chunks) for _, item in chunks)
        assert result["total_chunks"] == len(chunks)
        _assert_archive(
            {
                **result,
                "content": "".join(item["content"] for _, item in chunks),
            }
        )
    assert Path(runtime.env["TAR_CALLS"]).read_text() == "tar\n"
