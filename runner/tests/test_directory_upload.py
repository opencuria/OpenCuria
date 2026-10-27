"""Archive contents, responsiveness, cancellation and upload admission."""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.models import WorkspaceInfo
from src.services import files as files_module
from src.services.capture_fence import CaptureFence
from src.services.files import FileManager, convert_archive_to_tar
from src.services.workspace_registry import WorkspaceRegistry

from tests.directory_upload_runtime import LocalUploadRuntime


def _archive(mode: str = "w:gz", name: str = "project/source.txt") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode, format=tarfile.PAX_FORMAT) as archive:
        directory = tarfile.TarInfo("project/empty")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o750
        archive.addfile(directory)
        member = tarfile.TarInfo(name)
        member.size = len(b"source\n")
        member.mode = 0o751
        member.mtime = 123
        archive.addfile(member, io.BytesIO(b"source\n"))
    return buffer.getvalue()


@pytest.mark.parametrize("mode", ["w", "w:gz", "w:bz2", "w:xz"])
def test_conversion_preserves_members_and_metadata(mode: str) -> None:
    name = "project/" + "long-unicode-ä/" * 12 + "source.txt"
    result = convert_archive_to_tar(_archive(mode, name))
    with tarfile.open(fileobj=io.BytesIO(result)) as archive:
        assert archive.getnames() == ["project/empty", name]
        assert archive.getmember("project/empty").isdir()
        assert archive.getmember("project/empty").mode == 0o750
        member = archive.getmember(name)
        assert (member.mode, member.mtime) == (0o751, 123)
        stream = archive.extractfile(member)
        assert stream is not None
        assert stream.read() == b"source\n"


def _manager() -> tuple[FileManager, SimpleNamespace, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(workspace_id, "local", "running")

    async def execute(
        instance_id: str, command: list[str], **kwargs: object
    ) -> tuple[int, str]:
        return 0, command[2] if command[0] == "realpath" else ""

    runtime = SimpleNamespace(
        exec_command_wait=AsyncMock(side_effect=execute), put_archive=AsyncMock()
    )
    manager = FileManager(get_cached=lambda _: info, get_runtime=lambda _: runtime)
    manager.capture_fence = CaptureFence()
    return manager, runtime, workspace_id


async def test_directory_upload_uses_real_containment_and_tar_extraction() -> None:
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="upload-test-", dir=repo) as directory:
        runtime = LocalUploadRuntime()
        workspace_id = uuid.uuid4()
        info = WorkspaceInfo(workspace_id, "local", "running")
        manager = FileManager(get_cached=lambda _: info, get_runtime=lambda _: runtime)

        await manager.upload_file(
            workspace_id,
            directory,
            "project.tar.gz",
            base64.b64encode(_archive()).decode(),
            is_directory=True,
        )

        assert (Path(directory) / "project/source.txt").read_bytes() == b"source\n"
        assert (Path(directory) / "project/source.txt").stat().st_mode & 0o777 == 0o751
        assert (Path(directory) / "project/empty").is_dir()


@pytest.mark.parametrize("name", ["/etc/outside", "project/../../outside"])
async def test_unsafe_archive_is_never_uploaded(name: str) -> None:
    manager, runtime, workspace_id = _manager()
    with pytest.raises(ValueError, match="unsafe paths"):
        await manager.upload_file(
            workspace_id,
            "/workspace",
            "unsafe.tar.gz",
            base64.b64encode(_archive(name=name)).decode(),
            is_directory=True,
        )
    runtime.put_archive.assert_not_awaited()


async def test_failed_conversion_releases_worker_capacity() -> None:
    manager, runtime, workspace_id = _manager()
    with pytest.raises(tarfile.ReadError):
        await manager.upload_file(
            workspace_id,
            "/workspace",
            "invalid.tar.gz",
            base64.b64encode(b"invalid archive").decode(),
            is_directory=True,
        )
    runtime.put_archive.assert_not_awaited()
    await manager.upload_file(
        workspace_id,
        "/workspace",
        "valid.tar.gz",
        base64.b64encode(_archive()).decode(),
        is_directory=True,
    )
    runtime.put_archive.assert_awaited_once()


async def test_conversion_does_not_block_other_file_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, runtime, workspace_id = _manager()
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    threads = []

    def convert(content: bytes) -> bytes:
        threads.append(threading.get_ident())
        loop.call_soon_threadsafe(started.set)
        release.wait(1)
        return b"converted tar"

    monkeypatch.setattr(files_module, "convert_archive_to_tar", convert)
    task = asyncio.create_task(
        manager.upload_file(
            workspace_id,
            "/workspace",
            "project.tar.gz",
            "eA==",
            is_directory=True,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert len(threads) == 1
        assert threads[0] != threading.get_ident()
        assert not task.done()
        assert (
            await asyncio.wait_for(
                manager.list_files(uuid.uuid4(), "/workspace"), timeout=0.2
            )
            == []
        )
    finally:
        release.set()
        await task
    assert runtime.put_archive.await_args.args[2] == b"converted tar"


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancellation_drains_worker_keeps_budget_and_capture_admission(
    monkeypatch: pytest.MonkeyPatch,
    worker_fails: bool,
) -> None:
    manager, runtime, workspace_id = _manager()
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    calls = []

    def convert(content: bytes) -> bytes:
        calls.append(content)
        if content == b"first":
            loop.call_soon_threadsafe(started.set)
            release.wait(2)
            if worker_fails:
                raise ValueError("Conversion failed after cancellation")
        return content

    monkeypatch.setattr(files_module, "convert_archive_to_tar", convert)

    def upload(workspace: uuid.UUID, content: bytes) -> asyncio.Task:
        return asyncio.create_task(
            manager.upload_file(
                workspace,
                "/workspace",
                "project.tar.gz",
                base64.b64encode(content).decode(),
                is_directory=True,
            )
        )

    first = upload(workspace_id, b"first")
    second = queued = drain = None
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        assert not first.done()
        second = upload(uuid.uuid4(), b"second")
        queued = upload(uuid.uuid4(), b"queued")
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        for _ in range(2):
            first.cancel()
            await asyncio.sleep(0)
        assert not first.done()
        drain = asyncio.create_task(manager.capture_fence.drain(workspace_id))
        await asyncio.sleep(0)
        assert not drain.done()
        assert calls == [b"first"]
        runtime.put_archive.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(asyncio.gather(second, drain), timeout=2)
        assert calls == [b"first", b"second"]
        runtime.put_archive.assert_awaited_once()
        assert runtime.put_archive.await_args.args[2] == b"second"
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second, queued, drain) if task is not None),
            return_exceptions=True,
        )


async def test_lifecycle_change_during_conversion_rejects_stale_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, runtime, workspace_id = _manager()
    registry = WorkspaceRegistry(runtime=runtime)
    registry._cache[workspace_id] = WorkspaceInfo(workspace_id, "local", "running")
    manager.capture_fence = CaptureFence(registry)
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()

    def convert(content: bytes) -> bytes:
        loop.call_soon_threadsafe(started.set)
        release.wait(1)
        return content

    monkeypatch.setattr(files_module, "convert_archive_to_tar", convert)
    task = asyncio.create_task(
        manager.upload_file(
            workspace_id,
            "/workspace",
            "project.tar.gz",
            "eA==",
            is_directory=True,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        async with registry.lifecycle(workspace_id):
            pass
        release.set()
        with pytest.raises(RuntimeError, match="lifecycle changed"):
            await task
        runtime.put_archive.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
