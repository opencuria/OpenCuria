"""Batched list-entry metadata keeps HEAD, errors and lock semantics intact."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from src import git as git_ops
from src.git_reads import head_read_argv, parse_git_reads

from tests.test_git_operations import FakeGitRuntime, _make_repo
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git
from tests.test_git_snapshot_reads import MARKER, _frame, _service


@pytest.fixture(scope="module", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    """Keep all Git operations local to disposable fixtures."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


@pytest.mark.parametrize("state", ["branch", "detached", "unborn", "not-repo"])
def test_head_batch_matches_both_actual_commands(tmp_path: Path, state: str) -> None:
    """Do not infer HEAD state or lose diagnostics from either optional read."""
    if state in {"branch", "detached"}:
        _make_repo(str(tmp_path))
        argv = (
            ["git", "checkout", "--detach"]
            if state == "detached"
            else ["git", "checkout", "-b", "feature/é;$(touch_INJECTED)"]
        )
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True)
    elif state == "unborn":
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    def run(argv: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            argv,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        return result.returncode, result.stdout.decode()

    argv = head_read_argv(MARKER)
    argv[2] = argv[2].removeprefix(git_ops.GIT_SOURCE_ENV_SCRIPT)
    code, output = run(argv)
    assert code == 0
    assert parse_git_reads(output, MARKER, count=2) == [
        run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        run(["git", "rev-parse", "HEAD"]),
    ]
    assert not (tmp_path / "INJECTED").exists()


def test_head_credentials_and_marker_are_literal(tmp_path: Path) -> None:
    """Source credentials once, with guarded failures even under shell set -e."""
    source = tmp_path / "env file"
    source.write_text("set -e\nexport OC_HEAD_TEST=synthetic\n")
    executable = tmp_path / "git"
    executable.write_text(
        '#!/bin/sh\nprintf "%s" "$OC_HEAD_TEST"\n[ "$2" != "--abbrev-ref" ]\n'
    )
    executable.chmod(0o755)
    marker = "$(touch INJECTED); ' \""
    with patch.object(
        git_ops, "GIT_SOURCE_ENV_SCRIPT", f". {shlex.quote(str(source))}; "
    ):
        argv = head_read_argv(marker)
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.defpath},
    )
    assert result.returncode == 0
    assert parse_git_reads(result.stdout.decode(), marker, count=2) == [
        (1, "synthetic"),
        (0, "synthetic"),
    ]
    assert "synthetic" not in str(argv)
    assert not (tmp_path / "INJECTED").exists()


@pytest.mark.parametrize(
    "reads,branch,head",
    [
        ([(0, "feature\n"), (0, "b" * 40 + "\n")], "feature", "b" * 40),
        ([(0, "HEAD\n"), (0, "b" * 40 + "\n")], None, "b" * 40),
        ([(1, "file not found"), (0, "b" * 40 + "\n")], None, "b" * 40),
        ([(0, "feature\n"), (128, "fatal: unborn")], None, None),
        ([(128, "fatal: unborn"), (128, "fatal: unborn")], None, None),
    ],
)
async def test_list_entry_keeps_optional_read_results(
    reads: list[tuple[int, str]],
    branch: str | None,
    head: str | None,
) -> None:
    service, _ = _service(None)
    service._git_exec = AsyncMock(return_value=(0, _frame(reads)))
    with patch(
        "src.services.git_service.uuid.uuid4", return_value=SimpleNamespace(hex=MARKER)
    ):
        result = await service._git_list_entry(None, "instance", "/workspace/repo", {})
    assert result == {
        "id": "/workspace/repo",
        "name": "repo",
        "path": "/workspace/repo",
        "current_branch": branch,
        "head_hash": head,
    }
    service._git_exec.assert_awaited_once()


@pytest.mark.parametrize("failed_read", [0, 1])
@pytest.mark.parametrize("code", [126, 127])
async def test_missing_binary_preserves_structured_error(
    failed_read: int,
    code: int,
) -> None:
    reads = [(0, "main\n"), (0, "b" * 40 + "\n")]
    reads[failed_read] = (code, "git: not found")
    service, _ = _service(None)
    service._git_exec = AsyncMock(return_value=(0, _frame(reads)))
    with patch(
        "src.services.git_service.uuid.uuid4", return_value=SimpleNamespace(hex=MARKER)
    ):
        with pytest.raises(git_ops.GitError) as error:
            await service._git_list_entry(None, "instance", "/workspace/repo", {})
    assert error.value.code == "missing_git"
    assert error.value.exit_code == code
    assert error.value.stderr == "git: not found"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "unframed",
        _frame([(1, "failure")]),
        _frame([(0, "main")]),
        _frame([(0, "main"), (0, "head")])[:-1],
        _frame([(0, "main"), (0, "head")]) + "extra",
        _frame([(0, "main"), (0, "head"), (0, "extra")]),
        _frame([(0, "main"), (256, "head")]),
    ],
)
def test_head_frame_count_is_exact_even_after_first_read_fails(output: str) -> None:
    with pytest.raises(git_ops.GitError, match="Invalid Git snapshot"):
        parse_git_reads(output, MARKER, count=2)


async def test_list_rechecks_head_after_checkout(tmp_path: Path) -> None:
    """A later poll observes the new branch and commit without retained state."""
    repo = await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    runtime = FakeGitRuntime(str(tmp_path))
    service, workspace_id = _service(runtime)
    first = await service.execute_git_operation(workspace_id, "list_repos")
    for argv in (
        ["git", "checkout", "-b", "feature"],
        ["git", "commit", "--allow-empty", "-m", "New HEAD"],
    ):
        await asyncio.to_thread(
            subprocess.run, argv, cwd=repo, check=True, capture_output=True
        )
    second = await service.execute_git_operation(workspace_id, "list_repos")
    assert first["ok"] and second["ok"]
    assert first["repos"][0]["current_branch"] == "main"
    assert second["repos"][0]["current_branch"] == "feature"
    assert first["repos"][0]["head_hash"] != second["repos"][0]["head_hash"]


async def test_failed_batch_command_preserves_diagnostics() -> None:
    service, _ = _service(None)
    service._git_exec = AsyncMock(return_value=(2, "shell failed"))
    with pytest.raises(git_ops.GitError) as error:
        await service._git_list_entry(None, "instance", "/workspace/repo", {})
    assert error.value.code == "git_failed"
    assert error.value.exit_code == 2
    assert error.value.stderr == "shell failed"
    service._git_exec.assert_awaited_once()


async def test_head_timeout_has_no_retry() -> None:
    runtime = SimpleNamespace(exec_command_wait=AsyncMock(side_effect=TimeoutError()))
    service, _ = _service(runtime)
    with pytest.raises(git_ops.GitError) as error:
        await service._git_list_entry(runtime, "instance", "/workspace/repo", {})
    assert error.value.code == "timeout"
    runtime.exec_command_wait.assert_awaited_once()


async def test_cancelled_list_releases_repo_lock(tmp_path: Path) -> None:
    """A queued list remains serialized and finishes after caller cancellation."""
    await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    entered = asyncio.Event()
    batch_count = 0

    class HeldRuntime(FakeGitRuntime):
        async def exec_command_wait(
            self,
            instance_id: str,
            command: list[str],
            workdir: str | None = None,
            env: dict[str, str] | None = None,
        ) -> tuple[int, str]:
            nonlocal batch_count
            if command[3:4] == ["opencuria-git-head"]:
                batch_count += 1
                if batch_count == 1:
                    entered.set()
                    await asyncio.Event().wait()
            return await super().exec_command_wait(instance_id, command, workdir, env)

    runtime = HeldRuntime(str(tmp_path))
    service, workspace_id = _service(runtime)
    first = asyncio.create_task(
        service.execute_git_operation(workspace_id, "list_repos")
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(
            service.execute_git_operation(workspace_id, "list_repos")
        )
        lock = await service._git_lock(workspace_id, "/workspace/repo")
        assert lock.locked()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await asyncio.wait_for(second, 5)
        assert result["ok"], result
        assert batch_count == 2
        assert not lock.locked()
    finally:
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
