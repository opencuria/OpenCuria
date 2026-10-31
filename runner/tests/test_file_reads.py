"""File reads preserve guest bytes, framing, limits and bounded parsing work."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import tracemalloc
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from src.models import WorkspaceInfo
from src.services.files import FILE_READ_ABSOLUTE_MAX_SIZE, FileManager

from tests.directory_download_runtime import LocalFileRuntime


@pytest.fixture
def directory() -> Iterator[Path]:
    """Exercise real containment and guest commands inside /workspace."""
    with tempfile.TemporaryDirectory(
        prefix="file-read-test-", dir=Path(__file__).resolve().parents[2]
    ) as path:
        yield Path(path)


def _manager(runtime: object) -> tuple[FileManager, uuid.UUID]:
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
async def test_real_file_read_preserves_wrapped_base64(
    directory: Path, content: bytes
) -> None:
    """The full read path returns exactly the existing wrapped base64 payload."""
    source = directory / "file ' ; $name.bin"
    source.write_bytes(content)
    runtime = LocalFileRuntime()
    manager, workspace_id = _manager(runtime)

    result = await manager.read_file(workspace_id, str(source))

    assert result["content"] == base64.encodebytes(content).decode().strip()
    assert result["size"] == len(content)
    assert result["truncated"] is False
    assert result["mime_type"]
    assert len(runtime.calls) == 2


@pytest.mark.parametrize("limit", [1, 57, 58, 256, 257])
async def test_read_limit_and_truncation(directory: Path, limit: int) -> None:
    """Sizes describe the full source; content contains only the bounded prefix."""
    content = bytes(range(256))
    source = directory / "payload.bin"
    source.write_bytes(content)
    manager, workspace_id = _manager(LocalFileRuntime())

    result = await manager.read_file(workspace_id, str(source), max_size=limit)

    assert result["content"] == base64.encodebytes(content[:limit]).decode().strip()
    assert result["size"] == len(content)
    assert result["truncated"] is (limit < len(content))


async def test_concurrent_reads_keep_responses_independent(directory: Path) -> None:
    """Sharing the existing four-channel semaphore cannot mix file payloads."""
    sources = [directory / f"payload-{index}.bin" for index in range(8)]
    contents = [bytes([index]) * (64 * 1024 + index) for index in range(8)]
    for source, content in zip(sources, contents):
        source.write_bytes(content)
    manager, workspace_id = _manager(LocalFileRuntime())

    results = await asyncio.gather(
        *(manager.read_file(workspace_id, str(source)) for source in sources)
    )

    for result, content in zip(results, contents):
        assert result["content"] == base64.encodebytes(content).decode().strip()
        assert result["size"] == len(content)
        assert result["truncated"] is False


async def test_missing_file_is_still_a_read_error(directory: Path) -> None:
    manager, workspace_id = _manager(LocalFileRuntime())
    with pytest.raises(RuntimeError, match="Failed to read file:"):
        await manager.read_file(workspace_id, str(directory / "missing"))


@pytest.mark.parametrize("limit", [0, -1, FILE_READ_ABSOLUTE_MAX_SIZE + 1])
async def test_invalid_limit_does_not_contact_guest(limit: int) -> None:
    runtime = AsyncMock()
    manager, workspace_id = _manager(runtime)
    with pytest.raises(ValueError):
        await manager.read_file(workspace_id, "/workspace/payload.bin", max_size=limit)
    runtime.exec_command_wait.assert_not_awaited()


@pytest.mark.parametrize(
    "response,expected",
    [
        ("0\napplication/x-empty\n", (0, "application/x-empty", "")),
        ("0\n\n", (0, "application/octet-stream", "")),
        ("0\n ", (0, "application/octet-stream", "")),
        ("3\ntext/plain", (3, "text/plain", "")),
        (" 3 \n text/plain \n\nYWJj\n\n ", (3, "text/plain", "YWJj")),
        ("4\ntext/plain\nYWJj\nZA==\n", (4, "text/plain", "YWJj\nZA==")),
    ],
)
async def test_response_headers_and_payload(
    response: str, expected: tuple[int, str, str]
) -> None:
    """Header defaults, unterminated MIME lines and body trimming stay stable."""
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [(0, "/workspace/file\n"), (0, response)]
    manager, workspace_id = _manager(runtime)

    result = await manager.read_file(workspace_id, "/workspace/file")

    assert (result["size"], result["mime_type"], result["content"]) == expected


@pytest.mark.parametrize("response", ["", "3", "3\n", "\n", " 3 \n"])
async def test_missing_headers_still_raise(response: str) -> None:
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [(0, "/workspace/file\n"), (0, response)]
    manager, workspace_id = _manager(runtime)
    with pytest.raises(RuntimeError, match="Invalid file read response format"):
        await manager.read_file(workspace_id, "/workspace/file")


async def test_invalid_size_still_raises() -> None:
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [
        (0, "/workspace/file\n"),
        (0, "invalid\ntext/plain\nYWJj\n"),
    ]
    manager, workspace_id = _manager(runtime)
    with pytest.raises(ValueError):
        await manager.read_file(workspace_id, "/workspace/file")


async def test_large_read_response_allocation_budget() -> None:
    """Avoid one Python string/list entry per base64 line on the event loop.

    Fixture/transport allocations are excluded. There is room for a body slice
    and its trimmed result; the former parser needs more than 2.7 body copies.
    """
    content = bytes(range(256)) * (4 * 1024)
    encoded = base64.encodebytes(content).decode()
    response = f"{len(content)}\napplication/octet-stream\n{encoded}"
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [(0, "/workspace/file\n"), (0, response)]
    manager, workspace_id = _manager(runtime)

    tracemalloc.start()
    try:
        result = await manager.read_file(workspace_id, "/workspace/file")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["content"] == encoded.strip()
    assert peak < 2.25 * len(response)
