"""Bound guest exec amplification without weakening repository containment."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from src import git as git_ops
from src.models import WorkspaceInfo
from src.services.git_service import GitService

from tests.test_git_operations import (
    FakeGitRuntime,
    _inner_argv,
    _make_repo,
    _run_git,
)
from tests.test_git_operations import (
    setup_module as isolate_git,
)
from tests.test_git_operations import (
    teardown_module as restore_git,
)


@pytest.fixture(scope="module", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    """Use only disposable local Git configuration and repositories."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


def _service(runtime: FakeGitRuntime) -> tuple[GitService, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
    )
    return GitService(lambda _: info, lambda _: runtime), workspace_id


@pytest.mark.parametrize("repo_count", [1, 5, 15])
async def test_discovery_guest_exec_budget(tmp_path: Path, repo_count: int) -> None:
    """Each ordinary repo needs five execs, not eight, including its summary."""
    for index in range(repo_count):
        await asyncio.to_thread(_make_repo, str(tmp_path / f"repo-{index:02d}"))
    runtime = FakeGitRuntime(str(tmp_path))
    service, workspace_id = _service(runtime)

    result = await service.execute_git_operation(workspace_id, "list_repos")

    assert result["ok"], result
    assert [row["path"] for row in result["repos"]] == [
        f"/workspace/repo-{index:02d}" for index in range(repo_count)
    ]
    assert len(runtime.calls) == 5 * repo_count + 3
    for call in runtime.calls:
        argv = _inner_argv(call)
        if argv[:1] == ["git"]:
            assert argv[1] == "rev-parse"  # No status, history or ref enumeration.


async def test_identical_metadata_paths_resolved_once(tmp_path: Path) -> None:
    """One combined Git probe and one realpath suffice for ordinary metadata."""
    await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    runtime = FakeGitRuntime(str(tmp_path))
    service, _ = _service(runtime)

    root = await service._git_verify_repo_root(
        runtime, "instance-1", "/workspace/repo", {}
    )

    assert root == "/workspace/repo"
    assert len(runtime.calls) == 2
    assert _inner_argv(runtime.calls[0]) == [
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--absolute-git-dir",
        "--git-common-dir",
    ]
    assert _inner_argv(runtime.calls[1]) == ["realpath", "-m", "/workspace/repo/.git"]


async def test_worktree_resolves_both_distinct_metadata_paths(tmp_path: Path) -> None:
    """Linked worktrees must still containment-check gitdir AND common-dir."""
    repo = await asyncio.to_thread(_make_repo, str(tmp_path / "main"))
    code, output = await asyncio.to_thread(
        _run_git, ["git", "worktree", "add", str(tmp_path / "linked")], repo
    )
    assert code == 0, output
    runtime = FakeGitRuntime(str(tmp_path))
    service, _ = _service(runtime)

    root = await service._git_verify_repo_root(
        runtime, "instance-1", "/workspace/linked", {}
    )

    assert root == "/workspace/linked"
    resolved_paths = [
        _inner_argv(call)[2]
        for call in runtime.calls
        if _inner_argv(call)[:2] == ["realpath", "-m"]
    ]
    assert resolved_paths == [
        "/workspace/main/.git/worktrees/linked",
        "/workspace/main/.git",
    ]
    assert len(runtime.calls) == 3


@pytest.mark.parametrize(
    "output",
    [
        "",
        "/workspace/repo\n/workspace/repo/.git\n",  # Missing common-dir.
        "/workspace/repo\n/workspace/repo/.git\n/workspace/repo/.git\n/extra\n",
        "/workspace/other\n/workspace/repo/.git\n/workspace/repo/.git\n",
        "relative\n/workspace/repo/.git\n/workspace/repo/.git\n",
        "/workspace/repo\n\n/workspace/repo/.git\n",
        "/workspace/repo\n/workspace/repo/.git\nrelative\n",
        "/workspace/repo\x00\n/workspace/repo/.git\n/workspace/repo/.git\n",
    ],
)
async def test_malformed_combined_probe_rejected_before_metadata(output: str) -> None:
    """Do not resolve metadata or issue data commands after invalid probe output."""
    runtime = AsyncMock()
    runtime.exec_command_wait.return_value = (0, output)
    service, _ = _service(runtime)

    with pytest.raises(git_ops.GitError) as error:
        await service._git_verify_repo_root(
            runtime, "instance-1", "/workspace/repo", {}
        )

    assert error.value.code == "not_a_repo"
    assert runtime.exec_command_wait.await_count == 1


async def test_distinct_common_dir_escape_rejected() -> None:
    """A safe gitdir never makes a distinct external common-dir acceptable."""
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [
        (0, "/workspace/repo\n/workspace/repo/.git\n/outside/private.git\n"),
        (0, "/workspace/repo/.git\n"),
        (0, "/outside/private.git\n"),
    ]
    service, _ = _service(runtime)

    with pytest.raises(git_ops.GitError) as error:
        await service._git_verify_repo_root(
            runtime, "instance-1", "/workspace/repo", {}
        )

    assert error.value.code == "unsafe_repository"
    assert "/outside" not in str(error.value)
    assert runtime.exec_command_wait.await_count == 3


async def test_probe_timeout_retains_structured_error() -> None:
    """A failed combined probe must not fall back to unchecked metadata."""
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = TimeoutError
    service, _ = _service(runtime)

    with pytest.raises(git_ops.GitError) as error:
        await service._git_verify_repo_root(
            runtime, "instance-1", "/workspace/repo", {}
        )

    assert error.value.code == "timeout"
    assert runtime.exec_command_wait.await_count == 1


async def test_cancelled_probe_does_not_start_more_commands() -> None:
    """Cancellation still propagates without retries or follow-up probes."""
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_exec(*args: object, **kwargs: object) -> tuple[int, str]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return 0, ""

    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = blocked_exec
    service, _ = _service(runtime)
    task = asyncio.create_task(
        service._git_verify_repo_root(runtime, "instance-1", "/workspace/repo", {})
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancelled.is_set()
    assert runtime.exec_command_wait.await_count == 1


@pytest.mark.parametrize("has_commit", [False, True])
async def test_workspace_root_listed_once(tmp_path: Path, has_commit: bool) -> None:
    """Combining metadata reads must work for committed and unborn root repos."""
    if has_commit:
        await asyncio.to_thread(_make_repo, str(tmp_path))
    else:
        code, output = await asyncio.to_thread(
            _run_git, ["git", "init", "-b", "main", str(tmp_path)], str(tmp_path)
        )
        assert code == 0, output
    runtime = FakeGitRuntime(str(tmp_path))
    service, workspace_id = _service(runtime)

    result = await service.execute_git_operation(workspace_id, "list_repos")

    assert result["ok"], result
    assert len(result["repos"]) == 1
    assert result["repos"][0]["path"] == "/workspace"
    assert result["repos"][0]["current_branch"] == ("main" if has_commit else None)
    assert bool(result["repos"][0]["head_hash"]) is has_commit
    assert len(runtime.calls) == 6


async def test_discovery_rechecks_metadata_after_change(tmp_path: Path) -> None:
    """Do not retain trust across polls when a repo's metadata is replaced."""
    workspace = tmp_path / "workspace"
    await asyncio.to_thread(_make_repo, str(workspace / "repo"))
    runtime = FakeGitRuntime(str(workspace))
    service, workspace_id = _service(runtime)
    first = await service.execute_git_operation(workspace_id, "list_repos")
    assert first["ok"] and len(first["repos"]) == 1

    metadata = workspace / "repo" / ".git"
    external = tmp_path / "outside.git"
    metadata.rename(external)
    metadata.symlink_to(external, target_is_directory=True)
    runtime.calls.clear()
    result = await service.execute_git_operation(workspace_id, "list_repos")

    assert result["ok"] and result["repos"] == []
    assert any(_inner_argv(call)[:2] == ["realpath", "-m"] for call in runtime.calls)
