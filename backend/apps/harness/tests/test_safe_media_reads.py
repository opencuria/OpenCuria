"""Tests for symlink-safe bounded media reads over open_process."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from apps.harness.access.base import WorkspaceAccessor
from apps.harness.access.file_types import FileContent
from apps.harness.access.safe_files import (
    SAFE_MEDIA_READ_DEFAULT_MAX_SIZE,
    SafeFileReadError,
    read_file_no_follow,
)


class SubprocessByteStream:
    """Binary stdout stream backed by a local subprocess for filesystem tests."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.closed = False

    async def receive(self) -> bytes:
        assert self.process.stdout is not None
        return await self.process.stdout.read(64 * 1024)

    async def send_eof(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
            try:
                await self.process.stdin.wait_closed()
            except BrokenPipeError:
                pass

    async def aclose(self) -> None:
        self.closed = True
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 1)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def wait_closed(self) -> int | None:
        return await self.process.wait()


class LocalProcessAccessor:
    """Minimal production-like accessor: open_process runs argv in the workspace."""

    def __init__(self, *, on_open=None) -> None:
        self.on_open = on_open
        self.streams: list[SubprocessByteStream] = []
        self.calls: list[tuple[list[str], str, dict[str, str], float | None]] = []

    async def open_process(
        self,
        command: list[str],
        workdir: str,
        env: dict[str, str] | None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> SubprocessByteStream:
        # The test uses the current interpreter in place of the workspace's
        # python3 while preserving the safe-reader script and byte-stream API.
        argv = list(command)
        if argv[0] == "python3":
            argv[0] = sys.executable
        self.calls.append((argv, workdir, dict(env or {}), timeout))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stream = SubprocessByteStream(process)
        self.streams.append(stream)
        if self.on_open is not None:
            self.on_open.set()
        return stream


class HangingByteStream:
    def __init__(self) -> None:
        self.closed = False
        self.started = asyncio.Event()

    async def send_eof(self) -> None:
        return None

    async def receive(self) -> bytes:
        self.started.set()
        await asyncio.Event().wait()
        return b""

    async def wait_closed(self) -> int | None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class HangingAccessor:
    def __init__(self) -> None:
        self.stream = HangingByteStream()

    async def open_process(self, *_args: Any, **_kwargs: Any) -> HangingByteStream:
        return self.stream


@pytest.fixture
async def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


async def safe_read(
    accessor: Any, root: Path, path: str, max_size: int | None = None, **kwargs: Any
) -> FileContent:
    if path == "/workspace" or path.startswith("/workspace/"):
        path = str(root) + path[len("/workspace") :]
    return await read_file_no_follow(accessor, path, max_size, root=str(root), **kwargs)


@pytest.mark.parametrize("limit", [None, 3, 0])
async def test_accessor_default_safe_read_preserves_binary_metadata_and_closes_stream(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch, limit: int | None
) -> None:
    """The accessor's default reader preserves bounded bytes and file metadata."""
    monkeypatch.setattr(
        "apps.harness.access.base.HARNESS_WORKSPACE_ROOT", str(workspace_root)
    )
    payload = b"\x00\xff\x89PNG\r\n\x1a\n"
    path = workspace_root / "image.png"
    path.write_bytes(payload)
    accessor = LocalProcessAccessor()

    result = await WorkspaceAccessor.read_file_no_follow(accessor, str(path), limit)

    assert result == FileContent(
        content=payload if limit is None else payload[:limit],
        size=len(payload),
        truncated=limit is not None and limit < len(payload),
        mime="image/png",
    )
    assert accessor.calls[0][1:3] == (str(workspace_root), {})
    assert accessor.streams[0].closed


async def test_accessor_default_safe_read_rejects_paths_outside_its_root(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default accessor must forward its workspace root to the safe reader."""
    monkeypatch.setattr(
        "apps.harness.access.base.HARNESS_WORKSPACE_ROOT", str(workspace_root)
    )
    outside = workspace_root.parent / "outside.bin"
    outside.write_bytes(b"sensitive")
    accessor = LocalProcessAccessor()

    with pytest.raises(SafeFileReadError, match="Safe file read failed"):
        await WorkspaceAccessor.read_file_no_follow(accessor, str(outside))

    assert accessor.calls == []


@pytest.mark.asyncio
async def test_reads_binary_file_through_open_process(workspace_root: Path) -> None:
    payload = b"\x00\xff\x89PNG\r\n\x1a\n\x00"
    (workspace_root / "image.png").write_bytes(payload)
    accessor = LocalProcessAccessor()

    result = await safe_read(accessor, workspace_root, "/workspace/image.png")

    assert result == FileContent(
        content=payload, size=len(payload), truncated=False, mime="image/png"
    )
    assert len(accessor.calls) == 1
    command, workdir, env, _timeout = accessor.calls[0]
    assert command[0] == sys.executable
    assert workdir == str(workspace_root)
    assert env == {}
    assert "--" not in command
    assert accessor.streams[0].closed


@pytest.mark.asyncio
async def test_read_reports_truncation_and_file_size(workspace_root: Path) -> None:
    payload = bytes(range(256)) * 100
    (workspace_root / "large.bin").write_bytes(payload)

    result = await safe_read(
        LocalProcessAccessor(), workspace_root, "/workspace/large.bin", 37
    )

    assert result.content == payload[:37]
    assert result.size == len(payload)
    assert result.truncated is True


@pytest.mark.asyncio
async def test_zero_limit_and_empty_file_are_reported_accurately(
    workspace_root: Path,
) -> None:
    (workspace_root / "data.bin").write_bytes(b"data")
    (workspace_root / "empty.bin").write_bytes(b"")

    truncated = await safe_read(
        LocalProcessAccessor(), workspace_root, "/workspace/data.bin", 0
    )
    empty = await safe_read(
        LocalProcessAccessor(), workspace_root, "/workspace/empty.bin", 0
    )

    assert truncated.content == b""
    assert truncated.size == 4
    assert truncated.truncated is True
    assert empty.content == b""
    assert empty.size == 0
    assert empty.truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/workspace/../outside",
        "/workspace/a/../file",
        "../outside",
        "./file",
        "/workspace//file",
        "/workspace/file/",
    ],
)
async def test_rejects_paths_outside_or_not_lexically_normalized(
    workspace_root: Path, path: str
) -> None:
    accessor = LocalProcessAccessor()
    with pytest.raises(SafeFileReadError, match="Safe file read failed") as error:
        await safe_read(accessor, workspace_root, path)
    assert path not in str(error.value)
    assert accessor.calls == []


@pytest.mark.asyncio
async def test_rejects_leaf_and_ancestor_symlinks(workspace_root: Path) -> None:
    outside = workspace_root.parent / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"sensitive")
    (workspace_root / "leaf-link").symlink_to(outside / "secret.bin")
    (workspace_root / "dir-link").symlink_to(outside, target_is_directory=True)
    (workspace_root / "normal").mkdir()
    (workspace_root / "normal" / "leaf-link").symlink_to(outside / "secret.bin")

    for path in (
        "/workspace/leaf-link",
        "/workspace/dir-link/secret.bin",
        "/workspace/normal/leaf-link",
    ):
        with pytest.raises(SafeFileReadError, match="Safe file read failed") as error:
            await safe_read(LocalProcessAccessor(), workspace_root, path)
        assert "sensitive" not in str(error.value)
        assert str(outside) not in str(error.value)


@pytest.mark.asyncio
async def test_rejects_directories_and_missing_files(workspace_root: Path) -> None:
    (workspace_root / "directory").mkdir()
    for path in ("/workspace/directory", "/workspace/missing"):
        with pytest.raises(SafeFileReadError, match="Safe file read failed"):
            await safe_read(LocalProcessAccessor(), workspace_root, path)


@pytest.mark.asyncio
async def test_racing_directory_swap_never_reads_symlink_target(
    workspace_root: Path,
) -> None:
    local = workspace_root / "race"
    outside = workspace_root.parent / "outside-race"
    local.mkdir()
    outside.mkdir()
    (local / "file.bin").write_bytes(b"safe")
    (outside / "file.bin").write_bytes(b"outside secret")
    accessor = LocalProcessAccessor(on_open=asyncio.Event())
    read_task = asyncio.create_task(
        safe_read(accessor, workspace_root, "/workspace/race/file.bin")
    )
    # Start swapping only after the process exists, then keep racing the
    # component walk and open. The dir_fd walk can either pin the real local
    # directory or fail closed while a symlink/missing entry is present.
    await asyncio.wait_for(accessor.on_open.wait(), 2)

    def swap_for_a_bit() -> None:
        saved = workspace_root / "race-saved"
        link = workspace_root / "race-link"
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            try:
                local.rename(saved)
                link.symlink_to(outside, target_is_directory=True)
                link.rename(local)
                local.unlink()
                saved.rename(local)
            except FileNotFoundError:
                if saved.exists() and not local.exists():
                    saved.rename(local)
                if link.is_symlink():
                    link.unlink()

    # Run the mutator off-loop so subprocess output can be drained normally.
    await asyncio.to_thread(swap_for_a_bit)
    try:
        result = await asyncio.wait_for(read_task, 3)
    except SafeFileReadError:
        return
    assert result.content == b"safe"
    assert result.content != b"outside secret"


@pytest.mark.asyncio
async def test_default_limit_is_ten_mib(workspace_root: Path) -> None:
    payload = b"x" * (SAFE_MEDIA_READ_DEFAULT_MAX_SIZE + 1)
    (workspace_root / "large.bin").write_bytes(payload)

    result = await safe_read(
        LocalProcessAccessor(), workspace_root, "/workspace/large.bin"
    )

    assert len(result.content) == SAFE_MEDIA_READ_DEFAULT_MAX_SIZE
    assert result.size == SAFE_MEDIA_READ_DEFAULT_MAX_SIZE + 1
    assert result.truncated is True


@pytest.mark.asyncio
async def test_timeout_closes_process_and_returns_sanitized_error() -> None:
    accessor = HangingAccessor()
    with pytest.raises(SafeFileReadError, match="timed out") as error:
        await read_file_no_follow(
            accessor,
            "/workspace/file.bin",
            root="/workspace",
            timeout=0.05,
        )
    assert "file.bin" not in str(error.value)
    assert accessor.stream.closed


@pytest.mark.asyncio
async def test_cancellation_closes_process_and_propagates() -> None:
    accessor = HangingAccessor()
    task = asyncio.create_task(
        read_file_no_follow(
            accessor,
            "/workspace/file.bin",
            root="/workspace",
            timeout=5,
        )
    )
    await asyncio.wait_for(accessor.stream.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert accessor.stream.closed
