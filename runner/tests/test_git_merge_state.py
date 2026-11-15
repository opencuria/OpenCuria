"""Bound state-probe execs while preserving marker and containment semantics."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from src import git as git_ops
from src.models import WorkspaceInfo
from src.services.git_service import GitService

from tests.test_git_operations import FakeGitRuntime, _make_repo, _run_git
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git

NEUTRAL = {"merging": False, "rebasing": False, "cherry_picking": False}


@pytest.fixture(scope="module", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    """Keep scratch repositories independent of ambient Git configuration."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


def _service(runtime: FakeGitRuntime) -> tuple[GitService, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id, instance_id="instance-1", status="running"
    )
    return GitService(lambda _: info, lambda _: runtime), workspace_id


@pytest.mark.parametrize("markers", list(itertools.product([False, True], repeat=4)))
async def test_marker_combinations_in_one_probe(
    tmp_path: Path, markers: tuple[bool, bool, bool, bool]
) -> None:
    """Both rebase backends and simultaneous markers retain their meanings."""
    await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    names = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")
    for name, present in zip(names, markers, strict=True):
        if present:
            path = tmp_path / "repo" / ".git" / name
            if name.startswith("rebase-"):
                path.mkdir()
            else:
                path.touch()
    runtime = FakeGitRuntime(str(tmp_path))
    service, _ = _service(runtime)

    state = await service._git_merge_state(runtime, "instance-1", "/workspace/repo", {})

    assert state == {
        "merging": markers[0],
        "rebasing": markers[1] or markers[2],
        "cherry_picking": markers[3],
    }
    # rev-parse, realpath containment, and one marker command, even when idle.
    assert len(runtime.calls) == 3


@pytest.mark.parametrize("untracked_count", [0, 50, 500])
async def test_complete_snapshot_exec_budget(
    tmp_path: Path, untracked_count: int
) -> None:
    """Public snapshots include validation, refs, history and stash in 10 calls."""
    await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    for index in range(untracked_count):
        (tmp_path / "repo" / f"untracked-{index:04}.txt").touch()
    runtime = FakeGitRuntime(str(tmp_path))
    service, workspace_id = _service(runtime)

    result = await service.execute_git_operation(
        workspace_id, "repo_snapshot", "/workspace/repo", {}
    )

    assert result["ok"], result
    assert result["snapshot"]["merge_state"] == NEUTRAL
    assert result["snapshot"]["commits"]
    assert len(result["snapshot"]["changes"]) == untracked_count
    assert len(runtime.calls) == 10


async def test_worktree_uses_its_own_markers(tmp_path: Path) -> None:
    """A linked worktree reads its gitdir, never the common directory's state."""
    main = tmp_path / "main"
    worktree = tmp_path / "linked"
    await asyncio.to_thread(_make_repo, str(main))
    code, output = await asyncio.to_thread(
        _run_git,
        ["git", "worktree", "add", "-b", "linked", str(worktree)],
        str(main),
    )
    assert code == 0, output
    (main / ".git" / "MERGE_HEAD").touch()
    (main / ".git" / "worktrees" / "linked" / "CHERRY_PICK_HEAD").touch()
    runtime = FakeGitRuntime(str(tmp_path))
    service, _ = _service(runtime)

    state = await service._git_merge_state(
        runtime, "instance-1", "/workspace/linked", {}
    )

    assert state == {**NEUTRAL, "cherry_picking": True}


async def test_paths_remain_literal_shell_arguments(tmp_path: Path) -> None:
    """Quotes, spaces, command substitution and semicolons never execute."""
    name = "repo ' ; $(touch INJECTED) `touch ALSO_INJECTED`"
    await asyncio.to_thread(_make_repo, str(tmp_path / name))
    (tmp_path / name / ".git" / "MERGE_HEAD").touch()
    runtime = FakeGitRuntime(str(tmp_path))
    service, _ = _service(runtime)

    state = await service._git_merge_state(
        runtime, "instance-1", f"/workspace/{name}", {}
    )

    assert state == {**NEUTRAL, "merging": True}
    assert not list(tmp_path.rglob("*INJECTED"))


@pytest.mark.parametrize(
    "responses",
    [
        [(1, "failed")],
        [(0, "relative\n")],
        [(0, "/workspace/repo/.git\n/another/path\n")],
        [(0, "/workspace/repo/.git\x00\n")],
        [(0, "/workspace/repo/.git\n"), (1, "failed")],
        [(0, "/workspace/repo/.git\n"), (0, "/outside/private.git\n")],
        [(0, "/workspace/repo/.git\n"), (0, "\n")],
    ],
)
async def test_unverified_metadata_never_probed(
    responses: list[tuple[int, str]],
) -> None:
    """Failed or unsafe path checks return neutral state before any marker read."""
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = responses
    service, _ = _service(runtime)

    assert (
        await service._git_merge_state(runtime, "instance-1", "/workspace/repo", {})
        == NEUTRAL
    )
    assert runtime.exec_command_wait.await_count == len(responses)


@pytest.mark.parametrize("probe_result", [(1, "merging\n"), (0, "unexpected\n")])
async def test_failed_or_malformed_probe_returns_neutral(
    probe_result: tuple[int, str],
) -> None:
    """Do not publish partial or unrecognized output from a failed command."""
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [
        (0, "/workspace/repo/.git\n"),
        (0, "/workspace/repo/.git\n"),
        probe_result,
    ]
    service, _ = _service(runtime)

    assert (
        await service._git_merge_state(runtime, "instance-1", "/workspace/repo", {})
        == NEUTRAL
    )
    assert runtime.exec_command_wait.await_count == 3


async def test_markers_rechecked_after_change(tmp_path: Path) -> None:
    """Every snapshot rechecks current filesystem state without retained flags."""
    await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    runtime = FakeGitRuntime(str(tmp_path))
    service, _ = _service(runtime)
    marker = tmp_path / "repo" / ".git" / "MERGE_HEAD"
    for present in (False, True, False):
        if present:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)
        state = await service._git_merge_state(
            runtime, "instance-1", "/workspace/repo", {}
        )
        assert state == {**NEUTRAL, "merging": present}


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_probe_timeout_and_cancellation_propagate(
    failure: type[BaseException],
) -> None:
    """Keep structured timeouts and propagate cancellation without retries."""
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = [
        (0, "/workspace/repo/.git\n"),
        (0, "/workspace/repo/.git\n"),
        failure(),
    ]
    service, _ = _service(runtime)

    if failure is TimeoutError:
        with pytest.raises(git_ops.GitError) as error:
            await service._git_merge_state(runtime, "instance-1", "/workspace/repo", {})
        assert error.value.code == "timeout"
    else:
        with pytest.raises(asyncio.CancelledError):
            await service._git_merge_state(runtime, "instance-1", "/workspace/repo", {})
    assert runtime.exec_command_wait.await_count == 3
