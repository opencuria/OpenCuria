"""Real-shell coverage for bounded background process observations."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.service import WorkspaceService
from src.services.sessions.background import BackgroundProcess

from tests.background_status_runtime import LocalRuntime


@pytest.fixture
async def processes() -> AsyncIterator[tuple[int, int]]:
    """Provide live and reaped PIDs; never leave a child running after a test."""
    live = await asyncio.create_subprocess_exec("sleep", "600")
    dead = await asyncio.create_subprocess_exec("true")
    await dead.wait()
    try:
        yield live.pid, dead.pid
    finally:
        if live.returncode is None:
            live.terminate()
        await live.wait()


def _fixture(
    tmp_path: Path, pids: list[int]
) -> tuple[WorkspaceService, LocalRuntime, uuid.UUID, list[BackgroundProcess]]:
    runtime = LocalRuntime(str(tmp_path))
    service = WorkspaceService(
        {"docker": runtime}, RunnerSettings(state_dir=str(tmp_path), _env_file=None)
    )
    workspace_id = uuid.uuid4()
    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id, "instance-1", "running", runtime_type="docker"
    )
    entries = [
        BackgroundProcess(
            process_id=f"process-{index}",
            workspace_id=workspace_id,
            pid=pid,
            command=f"command {index}",
            workdir="/workspace",
            log_path=str(tmp_path / f"{index}.log"),
            exit_path=str(tmp_path / f"{index}.exit"),
            name=f"name {index}",
        )
        for index, pid in enumerate(pids)
    ]
    service.background._background_processes[workspace_id] = {
        entry.process_id: entry for entry in entries
    }
    return service, runtime, workspace_id, entries


async def test_mixed_list_matches_single_status_and_preserves_metadata(
    tmp_path: Path, processes: tuple[int, int]
) -> None:
    live, dead = processes
    service, runtime, workspace_id, entries = _fixture(
        tmp_path, [live, dead, dead, dead, dead, dead]
    )
    for index, value in (
        (0, "99"),
        (1, "  3\nignored"),
        (2, "0"),
        (3, "-9"),
        (4, "invalid"),
    ):
        Path(entries[index].exit_path).write_text(value)
    # The filename is passed as argv to cat, never interpolated into a shell.
    entries[2].exit_path = str(tmp_path / "'; touch injected; '.exit")
    Path(entries[2].exit_path).write_text("0")

    listed = await service.background.list_background_processes(workspace_id)

    assert runtime.calls == 6  # One liveness batch + five exit-file reads.
    assert [row["status"] for row in listed] == [
        "running",
        "exited",
        "exited",
        "exited",
        "unknown",
        "unknown",
    ]
    assert [row["exit_code"] for row in listed] == [None, 3, 0, -9, None, None]
    for row, entry in zip(listed, entries, strict=True):
        single = await service.background.get_background_status(
            workspace_id, entry.process_id
        )
        assert {key: row[key] for key in single} == single
        assert row["command"] == entry.command
        assert row["name"] == entry.name
        assert row["workdir"] == entry.workdir
        assert row["log_path"] == entry.log_path
        assert row["started_at"] == entry.started_at.isoformat()
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize(
    "count, expected_calls", [(0, 0), (1, 1), (100, 1), (101, 2), (205, 3)]
)
async def test_list_and_heartbeat_bound_runtime_calls(
    tmp_path: Path, processes: tuple[int, int], count: int, expected_calls: int
) -> None:
    service, runtime, workspace_id, entries = _fixture(tmp_path, [processes[0]] * count)

    listed = await service.background.list_background_processes(workspace_id)
    assert runtime.calls == expected_calls
    runtime.calls = 0
    heartbeat = (await service.registry.get_workspace_heartbeat_statuses())[0]
    assert runtime.calls == expected_calls
    for rows in (listed, heartbeat["processes"]):
        assert [row["process_id"] for row in rows] == [
            entry.process_id for entry in entries
        ]
        assert all(row["status"] == "running" for row in rows)


@pytest.mark.parametrize(
    "reply", [(1, "1\n1\n"), (0, "1\n"), (0, "1\ninvalid\n"), (0, "1\n1\n1\n")]
)
async def test_invalid_probe_never_confuses_unknown_with_exited(
    tmp_path: Path, processes: tuple[int, int], reply: tuple[int, str]
) -> None:
    service, runtime, workspace_id, entries = _fixture(tmp_path, [processes[0]] * 2)
    for entry in entries:
        Path(entry.exit_path).write_text("0")  # Stale evidence must stay unread.
    runtime.exec_command_wait = AsyncMock(return_value=reply)

    with pytest.raises(RuntimeError, match="Invalid background liveness response"):
        await service.background.list_background_processes(workspace_id)
    heartbeat = (await service.registry.get_workspace_heartbeat_statuses())[0]
    assert [row["status"] for row in heartbeat["processes"]] == ["unknown"] * 2
    assert all(row["exit_code"] is None for row in heartbeat["processes"])
    assert runtime.exec_command_wait.await_count == 2


async def test_heartbeat_isolates_failed_batch_and_exit_read(
    tmp_path: Path, processes: tuple[int, int]
) -> None:
    service, runtime, _workspace_id, entries = _fixture(
        tmp_path, [processes[0]] * 100 + [processes[1], processes[0]]
    )
    execute = runtime.exec_command_wait
    calls = 0

    async def fail_some(
        instance_id: str, command: list[str], **kwargs: Any
    ) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 1 or command == ["cat", entries[100].exit_path]:
            raise OSError("transport interrupted")
        return await execute(instance_id, command, **kwargs)

    runtime.exec_command_wait = fail_some
    heartbeat = (await service.registry.get_workspace_heartbeat_statuses())[0]
    assert [row["status"] for row in heartbeat["processes"]] == (
        ["unknown"] * 101 + ["running"]
    )
    assert calls == 3


async def test_cancellation_releases_admission_and_preserves_tracking(
    tmp_path: Path, processes: tuple[int, int]
) -> None:
    service, runtime, workspace_id, entries = _fixture(tmp_path, [processes[0]] * 2)
    started = asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> tuple[int, str]:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("Blocked probe unexpectedly resumed")

    runtime.exec_command_wait = blocked
    task = asyncio.create_task(
        service.background.list_background_processes(workspace_id)
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.capture_fence._users
    assert (
        list(service.background._background_processes[workspace_id].values()) == entries
    )


async def test_replacement_during_probe_keeps_snapshot_without_overwriting_winner(
    tmp_path: Path, processes: tuple[int, int]
) -> None:
    service, runtime, workspace_id, entries = _fixture(tmp_path, [processes[0]] * 2)
    execute = runtime.exec_command_wait
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> tuple[int, str]:
        started.set()
        await release.wait()
        return await execute(*args, **kwargs)

    runtime.exec_command_wait = blocked
    task = asyncio.create_task(
        service.background.list_background_processes(workspace_id)
    )
    await asyncio.wait_for(started.wait(), 2)
    winner = BackgroundProcess(
        process_id=entries[0].process_id,
        workspace_id=workspace_id,
        pid=processes[1],
        command="replacement",
        workdir="/workspace",
        log_path=entries[0].log_path,
        exit_path=entries[0].exit_path,
    )
    service.background._background_processes[workspace_id][winner.process_id] = winner
    release.set()
    prior = await task
    assert prior[0]["pid"] == entries[0].pid
    current = await service.background.list_background_processes(workspace_id)
    assert current[0]["pid"] == winner.pid
    assert current[0]["status"] == "unknown"
    assert (
        service.background._background_processes[workspace_id][winner.process_id]
        is winner
    )
