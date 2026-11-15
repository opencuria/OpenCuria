"""Bound list latency without losing containment, ordering or task ownership."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from src import git as git_ops
from src.models import WorkspaceInfo
from src.services.capture_fence import CaptureFence
from src.services.git_service import GitService

from tests.test_git_operations import FakeGitRuntime, _make_repo
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git


@pytest.fixture(scope="module", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    """Keep real fixture commands independent of ambient Git configuration."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


class ControlledRuntime(FakeGitRuntime):
    """Pause discovery or HEAD reads and observe their execution lifetime."""

    def __init__(self, root: str, phase: str) -> None:
        super().__init__(root)
        self.phase = phase
        self.release = asyncio.Event()
        self.four_active = asyncio.Event()
        self.peer_finished = asyncio.Event()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.active = 0
        self.peak = 0
        self.fail = False

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        path = command[2] if command[:2] == ["realpath", "-m"] else ""
        discovery = path.startswith("/workspace/repo-") and not path.endswith("/.git")
        head = command[3:4] == ["opencuria-git-head"]
        if not (discovery if self.phase == "discovery" else head):
            return await super().exec_command_wait(instance_id, command, workdir, env)
        path = path if discovery else str(workdir)
        self.started.append(path)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 4:
            self.four_active.set()
        try:
            await self.release.wait()
            if path.endswith("repo-00"):
                await self.peer_finished.wait()
            if self.fail and path.endswith(("repo-00", "repo-01")):
                # Unexpected discovery errors propagate; unsafe Git paths are
                # covered by the existing discovery/sandbox integration tests.
                if self.phase == "discovery":
                    raise RuntimeError(f"failed {path}")
                raise git_ops.GitError("git_failed", f"failed {path}")
            return await super().exec_command_wait(instance_id, command, workdir, env)
        finally:
            self.active -= 1
            self.finished.append(path)
            if not path.endswith("repo-00"):
                self.peer_finished.set()


GitSetup = tuple[ControlledRuntime, GitService, uuid.UUID]


@pytest.fixture(params=["discovery", "head"])
async def setup_git(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> GitSetup:
    """Use eight actual repositories, spanning two batches of parallel reads."""
    for index in range(8):
        await asyncio.to_thread(_make_repo, str(tmp_path / f"repo-{index:02d}"))
    runtime = ControlledRuntime(str(tmp_path), request.param)
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(workspace_id, "local", "running")
    return runtime, GitService(lambda _: info, lambda _: runtime), workspace_id


async def test_parallel_list_is_bounded_and_sorted(setup_git: GitSetup) -> None:
    """Later repositories can finish first without reordering the wire result."""
    runtime, service, workspace_id = setup_git
    task = asyncio.create_task(
        service.execute_git_operation(workspace_id, "list_repos")
    )
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        assert len(runtime.started) == 4
        runtime.release.set()
        result = await asyncio.wait_for(task, 5)
        assert result["ok"], result
        assert [row["path"] for row in result["repos"]] == [
            f"/workspace/repo-{index:02d}" for index in range(8)
        ]
        assert all(row["current_branch"] == "main" for row in result["repos"])
        assert runtime.finished.index("/workspace/repo-01") < runtime.finished.index(
            "/workspace/repo-00"
        )
        assert runtime.active == 0 and runtime.peak == 4
    finally:
        runtime.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_list_failure_drains_batch_before_releasing_context(
    setup_git: GitSetup,
) -> None:
    """Return the first repository's error only after every started read settles."""
    runtime, service, workspace_id = setup_git
    runtime.fail = True
    runtime.release.set()
    result = await asyncio.wait_for(
        service.execute_git_operation(workspace_id, "list_repos"), 5
    )
    prefix = "RuntimeError: " if runtime.phase == "discovery" else ""
    assert not result["ok"]
    assert result["message"] == prefix + "failed /workspace/repo-00"
    assert len(runtime.started) == len(runtime.finished) == 4
    assert runtime.active == 0
    runtime.fail = False
    assert (await service.execute_git_operation(workspace_id, "list_repos"))["ok"]


@pytest.mark.parametrize("termination", ["cancel", "timeout"])
async def test_terminated_list_releases_children_slots_and_locks(
    setup_git: GitSetup,
    monkeypatch: pytest.MonkeyPatch,
    termination: str,
) -> None:
    """An aborted read leaves no children running and the next list can finish."""
    runtime, service, workspace_id = setup_git
    if termination == "timeout":
        monkeypatch.setattr(git_ops, "GIT_READ_TIMEOUT_S", 0.5)
    task = asyncio.create_task(
        service.execute_git_operation(workspace_id, "list_repos")
    )
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 5)
            # Discovery skips invalid/timed-out candidates, as before.
            assert result["ok"] is (runtime.phase == "discovery")
            if runtime.phase == "head":
                assert result["code"] == "timeout"
        assert runtime.active == 0
        assert len(runtime.started) == len(runtime.finished)
        runtime.release.set()
        assert (await service.execute_git_operation(workspace_id, "list_repos"))["ok"]
    finally:
        runtime.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_list_budget_is_shared_across_workspaces(setup_git: GitSetup) -> None:
    """Two requests share four slots; cancelling a queued request cannot leak them."""
    runtime, service, workspace_id = setup_git
    queued = asyncio.Event()

    class ObservedSemaphore(asyncio.Semaphore):
        async def acquire(self) -> bool:
            if self.locked():
                queued.set()
            return await super().acquire()

    service._git_list_slots = ObservedSemaphore(4)
    tasks = [
        asyncio.create_task(service.execute_git_operation(workspace_id, "list_repos"))
    ]
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        tasks.append(
            asyncio.create_task(
                service.execute_git_operation(uuid.uuid4(), "list_repos")
            )
        )
        await asyncio.wait_for(queued.wait(), 5)
        assert runtime.active == 4 and len(runtime.started) == 4
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        assert runtime.active == 4
        runtime.release.set()
        assert (await asyncio.wait_for(tasks[0], 5))["ok"]
        assert (await service.execute_git_operation(uuid.uuid4(), "list_repos"))["ok"]
        assert runtime.active == 0 and runtime.peak == 4
    finally:
        runtime.release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cap_counts_verified_roots_and_skips_unsafe_metadata(
    setup_git: GitSetup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid candidate in the final batch must not hide a later safe root."""
    runtime, service, workspace_id = setup_git
    await asyncio.to_thread(_make_repo, str(tmp_path))
    metadata = tmp_path / "repo-00" / ".git"
    external = tmp_path.parent / f"outside-{uuid.uuid4().hex}.git"
    metadata.rename(external)
    metadata.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(git_ops, "GIT_DISCOVERY_MAX_REPOS", 3)
    runtime.release.set()
    try:
        result = await asyncio.wait_for(
            service.execute_git_operation(workspace_id, "list_repos"), 5
        )
        assert result["ok"], result
        assert [row["path"] for row in result["repos"]] == [
            "/workspace",
            "/workspace/repo-01",
            "/workspace/repo-02",
        ]
        assert str(external) not in str(result)
        assert not any(
            call[0][:3] == ["realpath", "-m", "/workspace/repo-03"]
            for call in runtime.calls
        )
    finally:
        # Keep all fixture cleanup in pytest's temporary tree.
        metadata.unlink()
        external.rename(metadata)


async def test_capture_remains_blocked_until_parallel_list_settles(
    setup_git: GitSetup,
) -> None:
    """Capture admission covers every child read, including failure drainage."""
    runtime, service, workspace_id = setup_git
    fence = service.capture_fence = CaptureFence()
    runtime.fail = True
    task = asyncio.create_task(
        service.execute_git_operation(workspace_id, "list_repos")
    )
    drain = None
    try:
        await asyncio.wait_for(runtime.four_active.wait(), 5)
        async with fence.capture(workspace_id):
            drain = asyncio.create_task(fence.drain(workspace_id))
            assert not drain.done()
            with pytest.raises(RuntimeError, match="capturing image"):
                await service.execute_git_operation(workspace_id, "list_repos")
            runtime.release.set()
            result = await asyncio.wait_for(task, 5)
            assert not result["ok"]
            await asyncio.wait_for(drain, 5)
            assert runtime.active == 0
    finally:
        runtime.release.set()
        for pending in (task, drain):
            if pending is not None:
                pending.cancel()
        await asyncio.gather(
            *(pending for pending in (task, drain) if pending is not None),
            return_exceptions=True,
        )
