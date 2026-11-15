"""Bounded untracked-diff execution through the public Git operation contract."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from src import git as git_ops
from src.models import WorkspaceInfo
from src.services.git_service import GitService

from tests.test_git_operations import FakeGitRuntime, _make_repo


class ControlledRuntime(FakeGitRuntime):
    """Hold real Git reads at a barrier and observe their task lifetimes."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.release = asyncio.Event()
        self.four_active = asyncio.Event()
        self.first_finished = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.fail = False

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        inner = git_ops.unwrap_git_exec_argv(command) or command
        if inner[:3] != ["git", "diff", "--no-index"]:
            return await super().exec_command_wait(instance_id, command, workdir, env)
        path = inner[-1]
        self.started.append(path)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 4:
            self.four_active.set()
        try:
            await self.release.wait()
            if path == "00 slow.txt":
                await self.first_finished.wait()
            if self.fail and path in {"00 slow.txt", "01.txt"}:
                raise git_ops.GitError("git_failed", f"failed {path}")
            if path == "08 unreadable.txt":
                return 2, "unreadable file"
            return await super().exec_command_wait(instance_id, command, workdir, env)
        finally:
            self.active -= 1
            self.finished.append(path)
            if path != "00 slow.txt":
                self.first_finished.set()


GitSetup = tuple[ControlledRuntime, uuid.UUID, WorkspaceInfo]


@pytest.fixture
def setup_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitSetup:
    """Create isolated repositories and deterministic text/binary input."""
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for name in ("repo", "other"):
        repo = Path(_make_repo(str(tmp_path / name)))
        for index in range(9):
            filename = {0: "00 slow.txt", 8: "08 unreadable.txt"}.get(
                index, f"{index:02d}.txt"
            )
            (repo / filename).write_bytes(
                b"\x00\xff" if index == 7 else f"file {index}\nsecond line\n".encode()
            )
    runtime = ControlledRuntime(str(tmp_path))
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id, instance_id="test", status="running"
    )
    return runtime, workspace_id, info


async def operation(
    service: GitService, workspace_id: uuid.UUID, repo: str = "repo"
) -> dict[str, Any]:
    """Call the same entry point used by the runner transport."""
    return await service.execute_git_operation(
        workspace_id, "working_diff", f"/workspace/{repo}", {}
    )


async def test_parallel_reads_preserve_sorted_text_binary_and_fallback(
    setup_git: GitSetup,
) -> None:
    runtime, workspace_id, info = setup_git
    service = GitService(lambda _: info, lambda _: runtime)
    task = asyncio.create_task(operation(service, workspace_id))
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        assert len(runtime.started) == 4  # Later batches have no tasks yet.
        runtime.release.set()
        result = await asyncio.wait_for(task, 5)
    finally:
        runtime.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert result["ok"]
    rows = result["diff"]["unstaged"]
    assert [row["new_path"] for row in rows] == sorted(runtime.started)
    assert runtime.finished.index("01.txt") < runtime.finished.index("00 slow.txt")
    assert runtime.active == 0 and runtime.peak == 4
    assert rows[0]["additions"] == 2
    assert "+file 0" in rows[0]["patch"]
    assert rows[7]["binary"]
    assert "patch" not in rows[8] and rows[8]["additions"] == 0


async def test_failure_drains_batch_and_reports_first_path_error(
    setup_git: GitSetup,
) -> None:
    runtime, workspace_id, info = setup_git
    service = GitService(lambda _: info, lambda _: runtime)
    runtime.fail = True
    runtime.release.set()
    result = await asyncio.wait_for(operation(service, workspace_id), 5)
    assert not result["ok"] and result["message"] == "failed 00 slow.txt"
    assert len(runtime.started) == 4 and len(runtime.finished) == 4
    assert runtime.active == 0
    # Both the repository lock and concurrency budget can be used again.
    runtime.fail = False
    assert (await operation(service, workspace_id))["ok"]


@pytest.mark.parametrize("termination", ["cancel", "timeout"])
async def test_terminated_batch_releases_tasks_slots_and_repo_lock(
    setup_git: GitSetup, monkeypatch: pytest.MonkeyPatch, termination: str
) -> None:
    runtime, workspace_id, info = setup_git
    service = GitService(lambda _: info, lambda _: runtime)
    if termination == "timeout":
        monkeypatch.setattr(git_ops, "GIT_READ_TIMEOUT_S", 0.5)
    task = asyncio.create_task(operation(service, workspace_id))
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 5)
            assert not result["ok"] and result["code"] == "timeout"
        assert runtime.active == 0 and len(runtime.finished) == 4
        runtime.release.set()
        assert (await operation(service, workspace_id))["ok"]
    finally:
        task.cancel()
        runtime.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrency_budget_is_shared_across_repositories(
    setup_git: GitSetup,
) -> None:
    runtime, workspace_id, info = setup_git
    eight_attempted = asyncio.Event()
    attempts = 0

    class ObservedService(GitService):
        """Observe queued reads without bypassing their execution budget."""

        async def _git_untracked_entry(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 8:
                eight_attempted.set()
            return await super()._git_untracked_entry(*args, **kwargs)

    service = ObservedService(lambda _: info, lambda _: runtime)
    tasks = [asyncio.create_task(operation(service, workspace_id))]
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        tasks.append(asyncio.create_task(operation(service, workspace_id, "other")))
        await asyncio.wait_for(eight_attempted.wait(), 5)
        assert runtime.active == 4 and runtime.peak == 4
        # The second batch is waiting for slots. Cancelling it must neither
        # leak its children nor disturb the first repository's active reads.
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        assert runtime.active == 4 and len(runtime.started) == 4
        runtime.release.set()
        assert (await asyncio.wait_for(tasks[0], 5))["ok"]
        assert (await operation(service, workspace_id, "other"))["ok"]
        assert runtime.active == 0 and runtime.peak == 4
    finally:
        runtime.release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
