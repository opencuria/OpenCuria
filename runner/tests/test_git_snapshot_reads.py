"""Snapshot batching preserves binary output, errors, security and repo locks."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from src import git as git_ops
from src.git_reads import parse_git_reads, snapshot_read_argv
from src.models import WorkspaceInfo
from src.services.git_service import GitService

from tests.test_git_operations import FakeGitRuntime, _make_repo
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git

MARKER = "a" * 32
STATUS = "# branch.oid " + "b" * 40 + "\0# branch.head main\0"


@pytest.fixture(scope="module", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    """Keep local Git configuration and identities disposable."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


def _frame(reads: list[tuple[int, str]]) -> str:
    return "".join(f"{output}\0{MARKER}:{code}\0" for code, output in reads)


def _service(runtime: object) -> tuple[GitService, uuid.UUID]:
    workspace_id = uuid.UUID(int=1)
    info = WorkspaceInfo(
        workspace_id=workspace_id, instance_id="instance-1", status="running"
    )
    return GitService(lambda _: info, lambda _: runtime), workspace_id


def _run(argv: list[str], cwd: Path, env: dict | None = None) -> tuple[int, str]:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=10)
    return result.returncode, (result.stdout + result.stderr).decode()


def test_actual_batch_matches_separate_commands_byte_for_byte(tmp_path: Path) -> None:
    """NULs, separators, Unicode and shell syntax in paths survive framing."""
    _make_repo(str(tmp_path))
    names = ["line\nbreak", "tab\tname", "quotes'\"", "$(touch INJECTED)", "é\x1e\x1f"]
    for name in names:
        (tmp_path / name).write_text("untracked\n")
    argv = snapshot_read_argv(MARKER)
    argv[2] = argv[2].removeprefix(git_ops.GIT_SOURCE_ENV_SCRIPT)
    code, output = _run(argv, tmp_path)
    expected = [
        _run(
            [
                "git",
                "status",
                "--porcelain=v2",
                "--branch",
                "--untracked-files=all",
                "-z",
            ],
            tmp_path,
        ),
        _run(
            [
                "git",
                "for-each-ref",
                f"--format={argv[-1]}",
                "refs/heads",
                "refs/remotes",
                "refs/tags",
            ],
            tmp_path,
        ),
        _run(["git", "remote"], tmp_path),
        _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], tmp_path),
    ]
    assert code == 0
    assert (
        parse_git_reads(output, MARKER, count=4, stop_on_first_error=True) == expected
    )
    assert not (tmp_path / "INJECTED").exists()


def test_failed_status_stops_actual_batch(tmp_path: Path) -> None:
    """No optional reads run after status fails, and its diagnostics survive."""
    argv = snapshot_read_argv(MARKER)
    argv[2] = argv[2].removeprefix(git_ops.GIT_SOURCE_ENV_SCRIPT)
    code, output = _run(argv, tmp_path)
    reads = parse_git_reads(output, MARKER, count=4, stop_on_first_error=True)
    assert code == 0
    assert len(reads) == 1
    assert reads[0][0] == 128
    assert "not a git repository" in reads[0][1]


def test_credentials_sourced_and_marker_stays_literal(tmp_path: Path) -> None:
    """Reuse credential sourcing without embedding secrets or arguments in code."""
    source = tmp_path / "env file"
    source.write_text("set -e\nexport OC_SNAPSHOT_TEST=synthetic-credential\n")
    executable = tmp_path / "git"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' \"$OC_SNAPSHOT_TEST\"\n"
        'case "$1" in for-each-ref) exit 7;; esac\n'
    )
    executable.chmod(0o755)
    marker = "$(touch INJECTED); ' \""
    with patch.object(
        git_ops, "GIT_SOURCE_ENV_SCRIPT", f". {shlex.quote(str(source))}; "
    ):
        argv = snapshot_read_argv(marker)
    code, output = _run(
        argv, tmp_path, {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.defpath}
    )
    assert code == 0
    assert parse_git_reads(output, marker, count=4, stop_on_first_error=True) == [
        (code, "synthetic-credential") for code in (0, 7, 0, 0)
    ]
    assert "synthetic-credential" not in str(argv)
    assert not (tmp_path / "INJECTED").exists()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "unframed",
        _frame([(0, "status")]),
        _frame([(0, "")] * 4)[:-1],
        _frame([(0, "")] * 4) + "extra",
        _frame([(1, "failed"), (0, "unexpected")]),
        _frame([(0, "")] * 5),
        _frame([(256, "bad")]),
        f"bad\0{MARKER}:no\0",
        f"bad\0{MARKER}:-1\0",
        f"bad\0{MARKER}:١\0",
        f"bad\0{MARKER}:0\0" + _frame([(0, "")] * 4),
    ],
)
def test_malformed_output_fails_closed(output: str) -> None:
    """Do not accept truncation, extra frames or an injected boundary."""
    with pytest.raises(git_ops.GitError, match="Invalid Git snapshot"):
        parse_git_reads(output, MARKER, count=4, stop_on_first_error=True)


@pytest.mark.parametrize("failed_read", [1, 2, 3])
async def test_optional_read_failure_preserves_other_fields(failed_read: int) -> None:
    """Optional failures keep their old empty-field behavior without a retry."""
    refs = git_ops._GIT_US.join(
        ["refs/heads/main", "main", "b" * 40, "commit", "", "", "*"]
    ) + git_ops._GIT_RS
    reads = [(0, STATUS), (0, refs), (0, "origin\n"), (0, "refs/remotes/origin/main\n")]
    reads[failed_read] = (1, "optional failure")
    service, _ = _service(None)
    service._git_exec = AsyncMock(return_value=(0, _frame(reads)))
    service._git_merge_state = AsyncMock(return_value={})
    with patch(
        "src.services.git_service.uuid.uuid4", return_value=SimpleNamespace(hex=MARKER)
    ):
        snapshot = await service._git_repo_snapshot_no_log(
            None, "instance", "/workspace/repo", {}
        )
    assert snapshot["current_branch"] == "main"
    assert len(snapshot["branches"]) == (0 if failed_read == 1 else 1)
    assert snapshot["remotes"] == ([] if failed_read == 2 else ["origin"])
    assert snapshot["default_remote"] == (
        None if failed_read == 3 else "refs/remotes/origin/main"
    )
    service._git_exec.assert_awaited_once()


@pytest.mark.parametrize(
    "exit_code,output,expected",
    [
        (129, "status failed", "git_failed"),
        (127, "git: not found", "missing_git"),
        (126, "git: no such file", "missing_git"),
        (1, "file not found", "git_failed"),
    ],
)
async def test_primary_error_keeps_code_and_diagnostics(
    exit_code: int,
    output: str,
    expected: str,
) -> None:
    service, _ = _service(None)
    service._git_exec = AsyncMock(return_value=(0, _frame([(exit_code, output)])))
    with patch(
        "src.services.git_service.uuid.uuid4", return_value=SimpleNamespace(hex=MARKER)
    ):
        with pytest.raises(git_ops.GitError) as error:
            await service._git_repo_snapshot_no_log(
                None, "instance", "/workspace/repo", {}
            )
    assert error.value.code == expected
    assert error.value.exit_code == exit_code
    assert error.value.stderr == output
    service._git_exec.assert_awaited_once()


async def test_snapshot_cancellation_releases_repo_lock(tmp_path: Path) -> None:
    """A queued snapshot stays serialized and can finish after cancellation."""
    await asyncio.to_thread(_make_repo, str(tmp_path / "repo"))
    entered = asyncio.Event()
    release = asyncio.Event()
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
            if command[3:4] == ["opencuria-git-snapshot"]:
                batch_count += 1
                if batch_count == 1:
                    entered.set()
                    await release.wait()
            return await super().exec_command_wait(instance_id, command, workdir, env)

    runtime = HeldRuntime(str(tmp_path))
    service, workspace_id = _service(runtime)
    first = asyncio.create_task(
        service.execute_git_operation(
            workspace_id, "repo_snapshot", "/workspace/repo", {}
        )
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(
            service.execute_git_operation(
                workspace_id, "repo_snapshot", "/workspace/repo", {}
            )
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
        release.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)


async def test_batch_timeout_keeps_structured_error() -> None:
    """No fallback/retry may hide a timed-out transport or leave a task running."""
    runtime = SimpleNamespace(exec_command_wait=AsyncMock(side_effect=TimeoutError()))
    service, _ = _service(runtime)
    with pytest.raises(git_ops.GitError) as error:
        await service._git_repo_snapshot_no_log(
            runtime, "instance", "/workspace/repo", {}
        )
    assert error.value.code == "timeout"
    runtime.exec_command_wait.assert_awaited_once()
