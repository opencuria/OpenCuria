"""File diff work must yield the loop without changing mutation contracts."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import file_diffs, files
from apps.harness.tools.base import ToolContext
from apps.harness.tools.file_locks import get_lock


def _arguments(tool_name: str) -> dict[str, Any]:
    if tool_name == "WriteTool":
        return {"path": "responsive.txt", "content": "new\n"}
    return {
        "path": "responsive.txt",
        "old_string": "old",
        "new_string": "new",
    }


@pytest.mark.parametrize("tool_name", ["WriteTool", "EditTool"])
async def test_pending_diff_keeps_loop_available(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop can run while a pure diff waits; tool completion still waits."""
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    original = file_diffs._build_patch_metadata

    def pending_diff(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Test did not release the diff worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(file_diffs, "_build_patch_metadata", pending_diff)
    accessor = FakeAccessor(files={"/workspace/responsive.txt": b"old\n"})
    ctx = ToolContext("s", "w", accessor)
    task = asyncio.create_task(
        getattr(files, tool_name)().execute(_arguments(tool_name), ctx)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert accessor.files["/workspace/responsive.txt"] == b"new\n"
        assert not task.done()
        if tool_name == "EditTool":
            assert get_lock("/workspace/responsive.txt").locked()
    finally:
        release.set()
        result = await task
    expected = original("/workspace/responsive.txt", old_text="old\n", new_text="new\n")
    if tool_name == "EditTool":
        expected["replacements"] = 1
    assert result.metadata == expected
    assert not get_lock("/workspace/responsive.txt").locked()


async def test_cancel_during_diff_releases_edit_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation leaves the committed write and no worker-owned mutation."""
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    original = file_diffs._build_patch_metadata

    def pending_diff(*args: Any, **kwargs: Any) -> dict[str, Any]:
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5), "Test did not release the diff worker"
            return original(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(file_diffs, "_build_patch_metadata", pending_diff)
    accessor = FakeAccessor(files={"/workspace/responsive.txt": b"old\n"})
    ctx = ToolContext("s", "w", accessor)
    task = asyncio.create_task(files.EditTool().execute(_arguments("EditTool"), ctx))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not get_lock("/workspace/responsive.txt").locked()
        monkeypatch.setattr(file_diffs, "_build_patch_metadata", original)
        followup = asyncio.create_task(
            files.EditTool().execute(
                {"path": "responsive.txt", "old_string": "new", "new_string": "next"},
                ctx,
            )
        )
        await asyncio.sleep(0)
        assert accessor.files["/workspace/responsive.txt"] == b"next\n"
        assert not followup.done()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    await followup
    assert accessor.files["/workspace/responsive.txt"] == b"next\n"


@pytest.mark.parametrize("tool_name", ["WriteTool", "EditTool"])
async def test_diff_failure_propagates_and_releases_lock(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker failures preserve normal exceptions and do not strand path locks."""

    def failed_diff(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("diff failed")

    monkeypatch.setattr(file_diffs, "_build_patch_metadata", failed_diff)
    accessor = FakeAccessor(files={"/workspace/responsive.txt": b"old\n"})
    ctx = ToolContext("s", "w", accessor)
    with pytest.raises(ValueError, match="diff failed"):
        await getattr(files, tool_name)().execute(_arguments(tool_name), ctx)
    assert accessor.files["/workspace/responsive.txt"] == b"new\n"
    assert not get_lock("/workspace/responsive.txt").locked()


async def test_cancel_queued_diff_skips_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a waiting diff removes its work before the worker is free."""
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    computed: list[str] = []
    original = file_diffs._build_patch_metadata

    def pending_diff(path: str, **kwargs: Any) -> dict[str, Any]:
        computed.append(path)
        if path == "active":
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "Test did not release the diff worker"
        return original(path, **kwargs)

    monkeypatch.setattr(file_diffs, "_build_patch_metadata", pending_diff)
    active = asyncio.create_task(
        file_diffs.build_patch_metadata("active", old_text="old", new_text="new")
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        queued = asyncio.create_task(
            file_diffs.build_patch_metadata("cancelled", old_text="old", new_text="new")
        )
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    finally:
        release.set()
        await active
    await file_diffs.build_patch_metadata("last", old_text="old", new_text="new")
    assert computed == ["active", "last"]
